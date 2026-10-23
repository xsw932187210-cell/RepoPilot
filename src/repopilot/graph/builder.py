from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from repopilot.approval_models import TestEvidence, evidence_identity
from repopilot.approvals import ApprovalConflict, ApprovalStore
from repopilot.candidates import ArtifactStore, Candidate, capture_manifest
from repopilot.config import Settings
from repopilot.data_boundary import (
    PublicExecutionError,
    SensitiveExecutionData,
    require_secret_free,
)
from repopilot.events import EventBus, JobQueue
from repopilot.execution import ExecutionStopped, current_control
from repopilot.generations import GenerationCoordinator, MemoryGenerationStore, SQLGenerationStore
from repopilot.github import GitHubPublisher, PublishResult
from repopilot.graph.privacy import PrivateExecutionGraph
from repopilot.graph.state import RepoPilotState
from repopilot.llm import AgentModel, remaining_model_calls
from repopilot.model_calls import ModelCallControlError, RateLimitError
from repopilot.models import CodeChangeOutput, FileEdit, PlanOutput, ReviewOutput, SandboxResult
from repopilot.policy_feedback import normalized_policy_feedback, validate_modification_intent
from repopilot.publication import PublicationStore
from repopilot.repository import RepositoryContext, WorkspaceManager
from repopilot.routing import WORKFLOW_MIN_CALLS, RoutingPolicy, decide_route
from repopilot.sandbox import DockerSandbox, LocalSandbox
from repopilot.security import SecurityError, redact_secrets

MAX_POLICY_RETRIES = 1


class TaskCancelled(ExecutionStopped):
    def __init__(self, message="cancel_requested"):
        super().__init__("cancel_requested")


def elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1_000))


def node_metric(node: str, duration_ms: int, iteration: int = 0) -> dict[str, Any]:
    return {"node": node, "duration_ms": duration_ms, "iteration": iteration}


def context_state(context: RepositoryContext) -> dict[str, Any]:
    return {
        "research_tree": context.tree,
        "research_files": context.files,
        "research_editable_paths": (
            list(context.editable_paths) if context.editable_paths is not None else None
        ),
        "research_evidence": context.evidence,
        "retrieval_query_terms": context.query_terms,
        "retrieval_strategy": context.strategy,
        "retrieval_candidate_count": context.candidate_count,
        "retrieval_selected_chars": context.selected_chars,
        "retrieval_skipped_for_budget": context.skipped_for_budget,
        "capability_policy_version": context.capability_policy_version,
    }


def repository_context_from_state(state: RepoPilotState, max_chars: int) -> RepositoryContext:
    return RepositoryContext(
        tree=state.get("research_tree", []),
        files=state.get("research_files", {}),
        editable_paths=(
            tuple(state["research_editable_paths"])
            if state.get("research_editable_paths") is not None
            else None
        ),
        evidence=state.get("research_evidence", []),
        query_terms=state.get("retrieval_query_terms", []),
        strategy=state.get("retrieval_strategy", "legacy"),
        candidate_count=state.get("retrieval_candidate_count", 0),
        selected_chars=state.get("retrieval_selected_chars", 0),
        skipped_for_budget=state.get("retrieval_skipped_for_budget", 0),
        max_chars=max_chars,
        capability_policy_version=state.get("capability_policy_version", "legacy"),
    )


@dataclass(slots=True)
class GraphDependencies:
    settings: Settings
    model: AgentModel
    workspaces: WorkspaceManager
    sandbox: LocalSandbox | DockerSandbox
    publisher: GitHubPublisher
    events: EventBus | None = None
    queue: JobQueue | None = None
    approvals: ApprovalStore | None = None
    generations: SQLGenerationStore | MemoryGenerationStore | None = None

    async def emit(
        self,
        state: RepoPilotState,
        node: str,
        message: str,
        payload: dict[str, Any] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        if self.events:
            event_payload = dict(payload or {})
            if duration_ms is not None:
                event_payload["duration_ms"] = duration_ms
            await self.events.publish(
                state["task_id"],
                kind="node_update",
                node=node,
                message=message,
                payload=event_payload,
            )

    async def ensure_active(self, state: RepoPilotState, *, check_generation: bool = True) -> None:
        control = current_control()
        if control:
            control.check()
        if not control and self.queue and await self.queue.is_cancelled(state["task_id"]):
            raise TaskCancelled("Task cancellation was requested")
        routing = state.get("routing")
        if routing and routing["policy_fingerprint"] != RoutingPolicy.from_settings(
            self.settings
        ).fingerprint:
            raise SecurityError("routing_policy_mismatch")
        if check_generation and self.generations and state.get("candidate"):
            await self.generations.assert_current(Candidate.model_validate(state["candidate"]))


def build_graph(deps: GraphDependencies, checkpointer: BaseCheckpointSaver):
    artifacts = ArtifactStore(deps.settings.workspace_root / ".artifacts")
    if deps.generations is None:
        deps.generations = (
            SQLGenerationStore(deps.approvals.database)
            if deps.approvals
            else MemoryGenerationStore()
        )
    generations = GenerationCoordinator(artifacts, deps.generations, deps.workspaces)

    async def remaining(state: RepoPilotState) -> int:
        used = sum(
            metric["node"] in {"planner", "coder", "reviewer"}
            for metric in state.get("node_metrics", [])
        )
        return await remaining_model_calls(deps.model, deps.settings.model_max_calls, used)

    def inspect_candidate(state: RepoPilotState, search_terms: list[str]) -> RepositoryContext:
        candidate = Candidate.model_validate(state["candidate"])
        with artifacts.materialize(candidate) as snapshot:
            context = deps.workspaces.inspect(
                snapshot,
                f"{state['issue_title']}\n{state['issue_body']}",
                search_terms,
            )
            if capture_manifest(snapshot, candidate.base_sha) != candidate.manifest:
                raise SecurityError("Candidate materialization changed during inspection")
        return context

    async def prepare(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        await deps.ensure_active(state, check_generation=False)
        workspace = await deps.workspaces.prepare(
            state["task_id"], state["repository_url"], state["base_branch"]
        )
        base_sha = await deps.workspaces.base_commit(workspace)
        candidate = await generations.initialize(state["task_id"], workspace, base_sha)
        duration_ms = elapsed_ms(started)
        await deps.emit(
            state,
            "prepare",
            "Prepared isolated repository workspace",
            duration_ms=duration_ms,
        )
        return {
            "workspace": str(workspace),
            "base_sha": base_sha,
            "candidate": candidate.model_dump(mode="json"),
            "status": "running",
            "iteration": 0,
            "node_metrics": [node_metric("prepare", duration_ms)],
        }

    async def planner(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        await deps.ensure_active(state)
        plan = await deps.model.plan(state["issue_title"], state["issue_body"])
        duration_ms = elapsed_ms(started)
        await deps.emit(
            state,
            "planner",
            "Planner produced a bounded execution plan",
            {"steps": plan.steps, "risks": plan.risk_notes},
            duration_ms,
        )
        return {
            "plan": plan.model_dump(),
            "node_metrics": [node_metric("planner", duration_ms)],
        }

    async def route(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        await deps.ensure_active(state)
        context = inspect_candidate(state, [])
        requested = state.get("execution_strategy", "workflow")
        decision = decide_route(
            requested, state["issue_title"], state["issue_body"], context,
            await remaining(state), RoutingPolicy.from_settings(deps.settings),
        )
        duration_ms = elapsed_ms(started)
        await deps.emit(state, "route", "Selected deterministic execution route", {
            "routing": decision,
        }, duration_ms)
        return {
            **context_state(context), "execution_strategy": requested,
            "routing": decision, "routing_history": [decision],
            "route_error_code": (
                decision["reasons"][0] if decision["selected"] == "stop" else None
            ),
            "initial_retrieval": {
                "selected_files": list(context.files), "query_terms": context.query_terms,
                "strategy": context.strategy, "candidate_count": context.candidate_count,
                "selected_chars": context.selected_chars,
                "skipped_for_budget": context.skipped_for_budget,
                "editable_files": list(context.editable_paths or ()),
                "capability_policy_version": context.capability_policy_version,
                "evidence": context.evidence,
            },
            "plan": PlanOutput(
                summary="Generate one minimal candidate",
                steps=["Fix the described behavior"],
            ).model_dump(),
            "node_metrics": [node_metric("route", duration_ms)],
        }

    def after_route(state: RepoPilotState) -> str:
        return {"oneshot": "coder", "workflow": "planner", "stop": "failed"}[
            state["routing"]["selected"]
        ]

    def oneshot(state: RepoPilotState) -> bool:
        return (state.get("routing") or {}).get("selected") == "oneshot"

    async def route_failure(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        await deps.ensure_active(state)
        decision = dict(state["routing"])
        calls = await remaining(state)
        error = state.get("policy_error_code") or "oneshot_validation_failed"
        safe_to_upgrade = error in {
            "oneshot_validation_failed", "invalid_modification_intent",
            "model_invalid_response", "policy_retry_exhausted",
        }
        can_upgrade = (
            decision["requested"] == "auto" and not decision["upgrades"]
            and safe_to_upgrade and state.get("iteration", 0) < state.get("max_iterations", 2)
            and calls >= WORKFLOW_MIN_CALLS
        )
        reason = (
            "oneshot_failed_upgrade_to_workflow" if can_upgrade else
            "insufficient_upgrade_budget" if decision["requested"] == "auto"
            and calls < WORKFLOW_MIN_CALLS else "oneshot_stopped"
        )
        decision.update(
            selected="workflow" if can_upgrade else "stop", reasons=[reason],
            remaining_calls=calls, upgrades=int(can_upgrade), trigger=error,
        )
        duration_ms = elapsed_ms(started)
        await deps.emit(state, "route_failure", "Resolved failed one-shot route", {
            "routing": decision,
        }, duration_ms)
        return {
            "routing": decision,
            "routing_history": [*state.get("routing_history", []), decision],
            "route_error_code": None if can_upgrade else reason,
            "policy_error_code": None if can_upgrade else state.get("policy_error_code"),
            "pending_changes": None,
            "reviewer_feedback": [
                *state.get("reviewer_feedback", []),
                "The first candidate did not pass visible validation. Inspect and correct it.",
            ],
            "node_metrics": [node_metric("route_failure", duration_ms, state.get("iteration", 0))],
        }

    async def researcher(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        await deps.ensure_active(state)
        plan = PlanOutput.model_validate(state["plan"])
        context = inspect_candidate(state, plan.search_terms)
        retrieval = {
            "selected_files": list(context.files),
            "query_terms": context.query_terms,
            "strategy": context.strategy,
            "candidate_count": context.candidate_count,
            "selected_chars": context.selected_chars,
            "skipped_for_budget": context.skipped_for_budget,
            "editable_files": list(context.editable_paths or ()),
            "capability_policy_version": context.capability_policy_version,
            "evidence": context.evidence,
        }
        duration_ms = elapsed_ms(started)
        await deps.emit(
            state,
            "researcher",
            f"Researcher selected {len(context.files)} relevant files",
            {"files": list(context.files), **retrieval},
            duration_ms,
        )
        return {
            **context_state(context),
            "initial_retrieval": state.get("initial_retrieval", retrieval),
            "node_metrics": [node_metric("researcher", duration_ms, state.get("iteration", 0))],
        }

    async def test_analyst(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        await deps.ensure_active(state)
        command = state["test_command"]
        strategy = f"Run `{command}` in a network-disabled container and require exit code 0."
        duration_ms = elapsed_ms(started)
        await deps.emit(
            state,
            "test_analyst",
            "Test analyst defined deterministic acceptance evidence",
            duration_ms=duration_ms,
        )
        return {
            "test_strategy": strategy,
            "node_metrics": [node_metric("test_analyst", duration_ms)],
        }

    async def coder(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        await deps.ensure_active(state)
        context = repository_context_from_state(state, deps.settings.max_context_chars)
        plan = PlanOutput.model_validate(state["plan"])
        if state.get("iteration", 0) > 0 or state.get("policy_feedback") is not None:
            context = inspect_candidate(state, plan.search_terms)
        model_feedback = list(state.get("reviewer_feedback", []))
        if state.get("policy_feedback") is not None:
            model_feedback.append(json.dumps(state["policy_feedback"], ensure_ascii=False))
        try:
            changes = CodeChangeOutput.model_validate(
                await deps.model.propose_changes(
                    state["issue_title"],
                    state["issue_body"],
                    plan,
                    context,
                    model_feedback[-10:],
                )
            )
        except RateLimitError:
            # The evaluation runtime must preserve quota-pending, including when
            # quota is encountered by Coder rather than Planner/Reviewer.
            raise
        except ModelCallControlError as error:
            duration_ms = elapsed_ms(started)
            await deps.emit(
                state,
                "coder",
                "Coder stopped at the controlled model-call boundary",
                {"error_code": error.code},
                duration_ms,
            )
            return {
                "pending_changes": None,
                "policy_error_code": error.code,
                "node_metrics": [node_metric("coder", duration_ms, state.get("iteration", 0))],
            }
        except ValidationError:
            duration_ms = elapsed_ms(started)
            await deps.emit(
                state,
                "coder",
                "Coder returned an invalid modification schema",
                {"error_code": "invalid_modification_intent"},
                duration_ms,
            )
            return {
                "pending_changes": None,
                "policy_error_code": "invalid_modification_intent",
                "node_metrics": [node_metric("coder", duration_ms, state.get("iteration", 0))],
            }
        try:
            require_secret_free(changes.model_dump())
        except SensitiveExecutionData:
            duration_ms = elapsed_ms(started)
            await deps.emit(
                state, "coder", "Modification intent contains sensitive execution data",
                {"error_code": "sensitive_execution_data"}, duration_ms,
            )
            return {
                "pending_changes": None,
                "policy_error_code": "sensitive_execution_data",
                "node_metrics": [node_metric("coder", duration_ms, state.get("iteration", 0))],
            }
        duration_ms = elapsed_ms(started)
        await deps.emit(
            state,
            "coder",
            "Coder submitted a structured modification intent",
            {"edit_count": len(changes.edits)},
            duration_ms,
        )
        return {
            **context_state(context),
            "pending_changes": changes.model_dump(),
            "policy_error_code": None,
            "node_metrics": [node_metric("coder", duration_ms, state.get("iteration", 0))],
        }

    def after_coder(state: RepoPilotState) -> str:
        if state.get("policy_error_code"):
            return "route_failure" if oneshot(state) else "failed"
        return "apply_policy"

    async def apply_policy(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        # A committed operation may be replayed after a checkpoint-write crash.
        # The coordinator resolves that exact operation before checking its parent.
        await deps.ensure_active(state, check_generation=False)
        if not state.get("candidate"):
            raise ApprovalConflict("legacy_approval_requires_revalidation")
        parent = Candidate.model_validate(state["candidate"])
        context = repository_context_from_state(state, deps.settings.max_context_chars)
        changes = CodeChangeOutput.model_validate(state["pending_changes"])
        try:
            validate_modification_intent(changes, context)
            candidate = await generations.apply(
                parent,
                changes.edits,
                expected_contents=context.files,
                base_repository=deps.workspaces.base_repository(Path(state["workspace"])),
            )
        except SecurityError as error:
            feedback = normalized_policy_feedback(error, context)
            used = state.get("policy_retry_count", 0)
            can_retry = used < MAX_POLICY_RETRIES
            duration_ms = elapsed_ms(started)
            await deps.emit(
                state,
                "apply_policy",
                "Complete modification intent rejected by workspace policy",
                {"feedback": feedback.model_dump(), "retry_available": can_retry},
                duration_ms,
            )
            return {
                "pending_changes": None,
                "policy_feedback": feedback.model_dump(),
                "policy_retry_count": used + int(can_retry),
                "policy_error_code": None if can_retry else "policy_retry_exhausted",
                "node_metrics": [
                    node_metric("apply_policy", duration_ms, state.get("iteration", 0))
                ],
            }
        edits_by_path = {
            edit.path: edit for edit in map(FileEdit.model_validate, state.get("edits", []))
        }
        edits_by_path.update({edit.path: edit for edit in changes.edits})
        iteration_changes = [
            edit.path for edit in changes.edits if context.files.get(edit.path) != edit.content
        ]
        changed_files = sorted(set(state.get("changed_files", [])) | set(iteration_changes))
        diff = artifacts.read_diff(candidate).decode("utf-8", errors="replace")[:100_000]
        iteration = candidate.generation
        duration_ms = elapsed_ms(started)
        await deps.emit(
            state,
            "apply_policy",
            f"Coder completed iteration {iteration} with {len(iteration_changes)} new file edits",
            {
                "changed_files": changed_files,
                "iteration_changes": iteration_changes,
                "summary": changes.summary,
                "policy_retry_count": state.get("policy_retry_count", 0),
            },
            duration_ms,
        )
        return {
            **context_state(context),
            "candidate": candidate.model_dump(mode="json"),
            "edits": [edit.model_dump() for edit in edits_by_path.values()],
            "changed_files": changed_files,
            "diff": diff,
            "iteration": iteration,
            "pending_changes": None,
            "policy_feedback": None,
            "policy_error_code": None,
            "node_metrics": [node_metric("apply_policy", duration_ms, iteration)],
        }

    def after_apply_policy(state: RepoPilotState) -> str:
        if oneshot(state) and (
            state.get("policy_error_code") or state.get("policy_feedback") is not None
        ):
            return "route_failure"
        if state.get("policy_error_code"):
            return "failed"
        return "coder" if state.get("policy_feedback") is not None else "test_runner"

    async def test_runner(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        await deps.ensure_active(state)
        candidate = Candidate.model_validate(state["candidate"])
        with artifacts.materialize(candidate) as snapshot:
            result = await deps.sandbox.run(snapshot, state["test_command"])
        # Diagnostic output is sanitized before binding evidence or checkpointing.
        result = result.model_copy(update={
            "stdout": redact_secrets(result.stdout),
            "stderr": redact_secrets(result.stderr),
        })
        await deps.generations.assert_current(candidate)
        evidence = TestEvidence(
            evidence_digest="",
            candidate_id=candidate.candidate_id,
            artifact_digest=candidate.artifact_digest,
            image_id=result.image_id,
            command=result.command,
            policy={
                "sandbox_version": result.policy_version,
                "command_version": "test-command-v1",
                "backend": deps.settings.sandbox_backend,
                "timeout_seconds": deps.settings.sandbox_timeout_seconds,
                "memory": deps.settings.sandbox_memory,
                "network_disabled": deps.settings.sandbox_backend == "docker",
                "workspace_policy_version": candidate.policy_version,
            },
            result=result,
            observed_at=datetime.now(UTC),
        )
        evidence = evidence.model_copy(update={"evidence_digest": evidence_identity(evidence)})
        if deps.approvals:
            await deps.approvals.record_evidence(state["task_id"], candidate, evidence)
        duration_ms = elapsed_ms(started)
        await deps.emit(
            state,
            "test_runner",
            "Sandbox tests passed" if result.passed else "Sandbox tests failed",
            {
                "candidate_id": candidate.candidate_id,
                "evidence_digest": evidence.evidence_digest,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "timed_out": result.timed_out,
            },
            duration_ms,
        )
        return {
            "candidate": candidate.model_dump(mode="json"),
            "test_evidence": evidence.model_dump(mode="json"),
            "diff": artifacts.read_diff(candidate).decode("utf-8", errors="replace")[:100_000],
            "test_result": result.model_dump(),
            "node_metrics": [node_metric("test_runner", duration_ms, state.get("iteration", 0))],
        }

    async def reviewer(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        await deps.ensure_active(state)
        result = SandboxResult.model_validate(state["test_result"])
        review = await deps.model.review(state["issue_title"], state.get("diff", ""), result)
        gate_feedback: list[str] = []
        if not result.passed:
            failure = "timed out" if result.timed_out else f"exited with code {result.exit_code}"
            gate_feedback.append(
                f"Deterministic gate: the sandbox test command {failure}. "
                "Resolve the test failure and rerun the configured command successfully."
            )
        if not state.get("diff", "").strip():
            gate_feedback.append(
                "Deterministic gate: no repository diff was produced. "
                "Produce a scoped code change that addresses the issue before requesting approval."
            )
        if gate_feedback:
            review = review.model_copy(
                update={
                    "approved": False,
                    "summary": "Deterministic validation rejected the candidate change.",
                    "feedback": (gate_feedback + review.feedback)[:10],
                    "risk_level": "high" if review.risk_level == "high" else "medium",
                }
            )
        duration_ms = elapsed_ms(started)
        deterministic_passed = result.passed and bool(state.get("diff", "").strip())
        can_retry_review = (
            deterministic_passed
            and not review.approved
            and review.risk_level in {"medium", "high"}
            and state.get("iteration", 0) < state.get("max_iterations", 2)
        )
        if review.approved:
            message = "Reviewer approved the candidate change"
        elif can_retry_review:
            message = "Reviewer requested one bounded remediation pass"
        elif deterministic_passed:
            message = "Reviewer flagged risk for the human approval decision"
        else:
            message = "Reviewer feedback will accompany a bounded deterministic retry"
        await deps.emit(
            state,
            "reviewer",
            message,
            {"approved": review.approved, "feedback": review.feedback},
            duration_ms,
        )
        return {
            "review": review.model_dump(),
            "review_performed": True,
            "reviewer_feedback": review.feedback,
            "node_metrics": [node_metric("reviewer", duration_ms, state.get("iteration", 0))],
        }

    async def oneshot_gate(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        await deps.ensure_active(state)
        result = SandboxResult.model_validate(state["test_result"])
        passed = result.passed and bool(state.get("diff", "").strip())
        feedback = [] if passed else [
            "Deterministic gate: visible tests failed or no repository diff was produced."
        ]
        return {
            "review_performed": False,
            "review": ReviewOutput(
                approved=False,
                summary="LLM review was not run. Human review of the tested candidate is required.",
                feedback=feedback, risk_level="low" if passed else "medium",
            ).model_dump(),
            "reviewer_feedback": feedback,
            "node_metrics": [node_metric("oneshot_gate", elapsed_ms(started), state["iteration"])],
        }

    def after_oneshot_gate(state: RepoPilotState) -> str:
        return "approval" if (
            SandboxResult.model_validate(state["test_result"]).passed
            and bool(state.get("diff", "").strip())
        ) else "route_failure"

    def after_review(state: RepoPilotState) -> str:
        tests_passed = SandboxResult.model_validate(state["test_result"]).passed
        review = ReviewOutput.model_validate(state["review"])
        # A speculative low-risk rejection is escalated to the human. One concrete
        # medium/high-risk finding may request bounded remediation; the next result
        # is escalated even if the model reviewer still objects.
        if tests_passed and state.get("diff", "").strip():
            if (
                not review.approved
                and review.risk_level in {"medium", "high"}
                and state.get("iteration", 0) < state.get("max_iterations", 2)
            ):
                return "coder"
            return "approval"
        if state.get("iteration", 0) < state.get("max_iterations", 2):
            return "coder"
        return "failed"

    async def approval(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        await deps.ensure_active(state)
        if "candidate" not in state or "test_evidence" not in state:
            raise ApprovalConflict("legacy_approval_requires_revalidation")
        candidate = Candidate.model_validate(state["candidate"])
        evidence = TestEvidence.model_validate(state["test_evidence"])
        if (
            evidence_identity(evidence) != evidence.evidence_digest
            or evidence.candidate_id != candidate.candidate_id
            or evidence.artifact_digest != candidate.artifact_digest
            or not evidence.result.passed
        ):
            raise ApprovalConflict("invalid_candidate_evidence_binding")
        decision = interrupt(
            {
                "schema_version": "approval-v2",
                "candidate_id": candidate.candidate_id,
                "artifact_digest": candidate.artifact_digest,
                "base_sha": candidate.base_sha,
                "evidence_digest": evidence.evidence_digest,
                "candidate_url": (
                    f"/api/v1/tasks/{state['task_id']}/candidates/{candidate.candidate_id}"
                ),
                "question": (
                    "Approve the verified change and allow the configured GitHub write tool?"
                ),
                "changed_files": state.get("changed_files", []),
                "review": state.get("review", {}),
                "review_performed": state.get("review_performed"),
                "routing": state.get("routing"),
                "test_result": state.get("test_result", {}),
                "diff_preview": state.get("diff", "")[:8_000],
            }
        )
        if (
            not isinstance(decision, dict)
            or decision.get("schema_version") != "approval-v2"
            or decision.get("candidate_id") != candidate.candidate_id
            or decision.get("evidence_digest") != evidence.evidence_digest
            or type(decision.get("approved")) is not bool
        ):
            raise ApprovalConflict("stale_approval_binding")
        if deps.approvals:
            saved = await deps.approvals.validate_resume(state["task_id"], decision)
            approved, feedback = saved.approved, saved.feedback
        else:
            if deps.settings.github_write_enabled:
                raise ApprovalConflict("persistent_approval_required_for_publication")
            approved, feedback = decision["approved"], str(decision.get("feedback", ""))
        duration_ms = elapsed_ms(started)
        return {
            "human_approved": approved,
            "human_feedback": feedback,
            "approval_decision_id": str(decision.get("decision_id", "offline-evaluation")),
            "publication_protocol_version": "publish-intent-v1",
            "status": "approved" if approved else "cancelled",
            "node_metrics": [node_metric("approval", duration_ms, state.get("iteration", 0))],
        }

    def after_approval(state: RepoPilotState) -> str:
        return "finalize" if state.get("human_approved") else "cancelled"

    async def finalize(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        await deps.ensure_active(state)
        artifacts.read_diff(Candidate.model_validate(state["candidate"]))
        artifacts.validate_edits(
            Candidate.model_validate(state["candidate"]), state.get("edits", [])
        )
        reason = deps.publisher.disabled_reason(state["repository_url"])
        if reason:
            result = PublishResult(published=False, reason=reason)
            if deps.approvals:
                store = PublicationStore(deps.approvals.database, artifacts)
                existing = await store.for_decision(
                    state["task_id"],
                    state.get("approval_decision_id", ""),
                )
                if existing:
                    deps.publisher.store = store
                    result = await deps.publisher.publish(intent_id=existing)
        else:
            if not deps.approvals:
                raise ApprovalConflict("persistent_approval_required_for_publication")
            if state.get("publication_protocol_version") != "publish-intent-v1":
                raise ApprovalConflict("legacy_publication_requires_reconciliation")
            store = PublicationStore(deps.approvals.database, artifacts)
            intent = await store.ensure(
                state["task_id"],
                state["approval_decision_id"],
                state["candidate"]["candidate_id"],
            )
            deps.publisher.store = store

            async def cancelled() -> bool:
                control = current_control()
                if control:
                    control.remaining()
                    return bool(control.reason)
                return bool(deps.queue and await deps.queue.is_cancelled(state["task_id"]))

            result = await deps.publisher.publish(
                intent_id=intent.publish_intent_id,
                cancelled=cancelled,
            )
        receipt = result.receipt.model_dump(mode="json") if result.receipt else None
        status = "completed"
        if result.receipt and result.receipt.phase != "published":
            status = "cancelled" if result.receipt.cancel_requested else "failed"
        duration_ms = elapsed_ms(started)
        await deps.emit(
            state,
            "finalize",
            "Publication reconciled" if receipt else "Verified artifact is ready",
            {
                "pull_request_url": result.pull_request_url,
                "branch": result.branch,
                "publish_reason": result.reason,
                "publish_receipt": receipt,
            },
            duration_ms,
        )
        return {
            "status": status,
            "pull_request_url": result.pull_request_url,
            "publish_receipt": receipt,
            "publish_reason": result.reason,
            **({"error": result.reason} if status == "failed" else {}),
            "node_metrics": [node_metric("finalize", duration_ms, state.get("iteration", 0))],
        }

    async def failed(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        duration_ms = elapsed_ms(started)
        await deps.emit(
            state,
            "failed",
            "Task stopped at a bounded failure condition",
            (
                {
                    "iteration": state.get("iteration", 0),
                    "policy_feedback": state.get("policy_feedback"),
                    "error_code": state["policy_error_code"],
                }
                if state.get("policy_error_code")
                else {
                    "iteration": state.get("iteration", 0),
                    "feedback": state.get("reviewer_feedback", []),
                }
            ),
            duration_ms,
        )
        return {
            "status": "failed",
            "error": state.get("route_error_code") or state.get("policy_error_code")
            or "Candidate failed deterministic or reviewer validation within the retry budget",
            "node_metrics": [node_metric("failed", duration_ms, state.get("iteration", 0))],
        }

    async def cancelled(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        duration_ms = elapsed_ms(started)
        await deps.emit(
            state,
            "cancelled",
            "Human rejected the side-effecting action",
            duration_ms=duration_ms,
        )
        return {
            "status": "cancelled",
            "error": state.get("human_feedback", ""),
            "node_metrics": [node_metric("cancelled", duration_ms, state.get("iteration", 0))],
        }

    def interruptible(node, *, cancellable=True):
        async def execute(state):
            try:
                control = current_control()
                if control and cancellable:
                    result = await control.run(node(state))
                else:
                    result = await node(state)
            except GraphBubbleUp:
                raise
            except Exception as error:
                if type(error).__module__.startswith("repopilot."):
                    # Preserve application control-flow types only when their
                    # checkpoint representation contains no known credentials.
                    try:
                        require_secret_free(str(error))
                    except SecurityError:
                        pass
                    else:
                        raise
                raise PublicExecutionError(error) from None
            require_secret_free(result)
            return result

        return execute

    graph = StateGraph(RepoPilotState)
    graph.add_node("prepare", interruptible(prepare))
    graph.add_node("route", interruptible(route))
    graph.add_node("route_failure", interruptible(route_failure))
    graph.add_node("oneshot_gate", interruptible(oneshot_gate))
    graph.add_node("planner", interruptible(planner))
    graph.add_node("researcher", interruptible(researcher))
    graph.add_node("test_analyst", interruptible(test_analyst))
    graph.add_node("coder", interruptible(coder))
    graph.add_node("apply_policy", interruptible(apply_policy))
    graph.add_node("test_runner", interruptible(test_runner))
    graph.add_node("reviewer", interruptible(reviewer))
    graph.add_node("approval", interruptible(approval))
    # Publication retains its bounded reconciliation window after a stop.
    graph.add_node("finalize", interruptible(finalize, cancellable=False))
    graph.add_node("failed", interruptible(failed))
    graph.add_node("cancelled", interruptible(cancelled))

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "route")
    graph.add_conditional_edges(
        "route", after_route, {"coder": "coder", "planner": "planner", "failed": "failed"}
    )
    graph.add_conditional_edges(
        "route_failure", lambda state: "planner" if state["routing"]["selected"] == "workflow"
        else "failed", {"planner": "planner", "failed": "failed"},
    )
    graph.add_edge("planner", "researcher")
    graph.add_edge("planner", "test_analyst")
    graph.add_edge(["researcher", "test_analyst"], "coder")
    graph.add_conditional_edges(
        "coder", after_coder,
        {"apply_policy": "apply_policy", "failed": "failed", "route_failure": "route_failure"},
    )
    graph.add_conditional_edges(
        "apply_policy",
        after_apply_policy,
        {"coder": "coder", "test_runner": "test_runner", "failed": "failed",
         "route_failure": "route_failure"},
    )
    graph.add_conditional_edges(
        "test_runner", lambda state: "oneshot_gate" if oneshot(state) else "reviewer",
        {"oneshot_gate": "oneshot_gate", "reviewer": "reviewer"},
    )
    graph.add_conditional_edges(
        "oneshot_gate", after_oneshot_gate,
        {"approval": "approval", "route_failure": "route_failure"},
    )
    graph.add_conditional_edges(
        "reviewer",
        after_review,
        {"approval": "approval", "coder": "coder", "failed": "failed"},
    )
    graph.add_conditional_edges(
        "approval", after_approval, {"finalize": "finalize", "cancelled": "cancelled"}
    )
    graph.add_edge("finalize", END)
    graph.add_edge("failed", END)
    graph.add_edge("cancelled", END)
    return PrivateExecutionGraph(graph.compile(checkpointer=checkpointer))
