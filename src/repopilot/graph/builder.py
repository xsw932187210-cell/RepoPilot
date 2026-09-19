from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from repopilot.config import Settings
from repopilot.events import EventBus, JobQueue
from repopilot.github import GitHubPublisher
from repopilot.graph.state import RepoPilotState
from repopilot.llm import AgentModel
from repopilot.models import FileEdit, PlanOutput, ReviewOutput, SandboxResult
from repopilot.repository import RepositoryContext, WorkspaceManager
from repopilot.sandbox import DockerSandbox, LocalSandbox
from repopilot.security import SecurityError

MAX_POLICY_RETRIES = 1


class TaskCancelled(RuntimeError):
    pass


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


@dataclass(slots=True)
class GraphDependencies:
    settings: Settings
    model: AgentModel
    workspaces: WorkspaceManager
    sandbox: LocalSandbox | DockerSandbox
    publisher: GitHubPublisher
    events: EventBus | None = None
    queue: JobQueue | None = None

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

    async def ensure_active(self, state: RepoPilotState) -> None:
        if self.queue and await self.queue.is_cancelled(state["task_id"]):
            raise TaskCancelled("Task cancellation was requested")


def build_graph(deps: GraphDependencies, checkpointer: BaseCheckpointSaver):
    async def prepare(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        await deps.ensure_active(state)
        workspace = await deps.workspaces.prepare(
            state["task_id"], state["repository_url"], state["base_branch"]
        )
        duration_ms = elapsed_ms(started)
        await deps.emit(
            state,
            "prepare",
            "Prepared isolated repository workspace",
            duration_ms=duration_ms,
        )
        return {
            "workspace": str(workspace),
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

    async def researcher(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        await deps.ensure_active(state)
        plan = PlanOutput.model_validate(state["plan"])
        context = deps.workspaces.inspect(
            Path(state["workspace"]),
            f"{state['issue_title']}\n{state['issue_body']}",
            plan.search_terms,
        )
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
            "initial_retrieval": retrieval,
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
        context = RepositoryContext(
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
            max_chars=deps.settings.max_context_chars,
            capability_policy_version=state.get("capability_policy_version", "legacy"),
        )
        plan = PlanOutput.model_validate(state["plan"])
        if state.get("iteration", 0) > 0:
            context = deps.workspaces.inspect(
                Path(state["workspace"]),
                f"{state['issue_title']}\n{state['issue_body']}",
                plan.search_terms,
            )
        model_feedback = list(state.get("reviewer_feedback", []))
        policy_retries = 0
        while True:
            changes = await deps.model.propose_changes(
                state["issue_title"],
                state["issue_body"],
                plan,
                context,
                model_feedback,
            )
            try:
                iteration_changes = deps.workspaces.apply_edits(
                    Path(state["workspace"]),
                    changes.edits,
                    expected_contents=context.files,
                )
                break
            except SecurityError as error:
                if policy_retries >= MAX_POLICY_RETRIES:
                    raise
                policy_retries += 1
                model_feedback = [
                    *model_feedback,
                    (
                        f"Tool policy rejected the complete proposal: {error}. "
                        "No edits were applied. Retry using only exact allowed paths and "
                        "the latest supplied file contents."
                    ),
                ][-10:]
                context = deps.workspaces.inspect(
                    Path(state["workspace"]),
                    f"{state['issue_title']}\n{state['issue_body']}",
                    plan.search_terms,
                )
        edits_by_path = {
            edit.path: edit for edit in map(FileEdit.model_validate, state.get("edits", []))
        }
        edits_by_path.update({edit.path: edit for edit in changes.edits})
        changed_files = sorted(set(state.get("changed_files", [])) | set(iteration_changes))
        diff = await deps.workspaces.diff(Path(state["workspace"]))
        iteration = state.get("iteration", 0) + 1
        duration_ms = elapsed_ms(started)
        await deps.emit(
            state,
            "coder",
            f"Coder completed iteration {iteration} with {len(iteration_changes)} new file edits",
            {
                "changed_files": changed_files,
                "iteration_changes": iteration_changes,
                "summary": changes.summary,
                "policy_retries": policy_retries,
            },
            duration_ms,
        )
        return {
            **context_state(context),
            "edits": [edit.model_dump() for edit in edits_by_path.values()],
            "changed_files": changed_files,
            "diff": diff,
            "iteration": iteration,
            "policy_retry_count": state.get("policy_retry_count", 0) + policy_retries,
            "node_metrics": [node_metric("coder", duration_ms, iteration)],
        }

    async def test_runner(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        await deps.ensure_active(state)
        result = await deps.sandbox.run(Path(state["workspace"]), state["test_command"])
        duration_ms = elapsed_ms(started)
        await deps.emit(
            state,
            "test_runner",
            "Sandbox tests passed" if result.passed else "Sandbox tests failed",
            {
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "timed_out": result.timed_out,
            },
            duration_ms,
        )
        return {
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
            "reviewer_feedback": review.feedback,
            "node_metrics": [node_metric("reviewer", duration_ms, state.get("iteration", 0))],
        }

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

    def approval(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        decision = interrupt(
            {
                "question": (
                    "Approve the verified change and allow the configured GitHub write tool?"
                ),
                "changed_files": state.get("changed_files", []),
                "review": state.get("review", {}),
                "test_result": state.get("test_result", {}),
                "diff_preview": state.get("diff", "")[:8_000],
            }
        )
        approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
        feedback = str(decision.get("feedback", "")) if isinstance(decision, dict) else ""
        duration_ms = elapsed_ms(started)
        return {
            "human_approved": approved,
            "human_feedback": feedback,
            "status": "approved" if approved else "cancelled",
            "node_metrics": [node_metric("approval", duration_ms, state.get("iteration", 0))],
        }

    def after_approval(state: RepoPilotState) -> str:
        return "finalize" if state.get("human_approved") else "cancelled"

    async def finalize(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        edits = [FileEdit.model_validate(item) for item in state.get("edits", [])]
        result = await deps.publisher.publish(
            task_id=state["task_id"],
            repository_url=state["repository_url"],
            base_branch=state["base_branch"],
            issue_title=state["issue_title"],
            edits=edits,
        )
        duration_ms = elapsed_ms(started)
        await deps.emit(
            state,
            "finalize",
            "Draft pull request created" if result.published else "Verified artifact is ready",
            {
                "pull_request_url": result.pull_request_url,
                "branch": result.branch,
                "publish_reason": result.reason,
            },
            duration_ms,
        )
        return {
            "status": "completed",
            "pull_request_url": result.pull_request_url,
            "node_metrics": [node_metric("finalize", duration_ms, state.get("iteration", 0))],
        }

    async def failed(state: RepoPilotState) -> dict[str, Any]:
        started = time.perf_counter()
        duration_ms = elapsed_ms(started)
        await deps.emit(
            state,
            "failed",
            "Task stopped after the bounded retry budget",
            {
                "iteration": state.get("iteration", 0),
                "feedback": state.get("reviewer_feedback", []),
            },
            duration_ms,
        )
        return {
            "status": "failed",
            "error": (
                "Candidate failed deterministic or reviewer validation within the retry budget"
            ),
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

    graph = StateGraph(RepoPilotState)
    graph.add_node("prepare", prepare)
    graph.add_node("planner", planner)
    graph.add_node("researcher", researcher)
    graph.add_node("test_analyst", test_analyst)
    graph.add_node("coder", coder)
    graph.add_node("test_runner", test_runner)
    graph.add_node("reviewer", reviewer)
    graph.add_node("approval", approval)
    graph.add_node("finalize", finalize)
    graph.add_node("failed", failed)
    graph.add_node("cancelled", cancelled)

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "planner")
    graph.add_edge("planner", "researcher")
    graph.add_edge("planner", "test_analyst")
    graph.add_edge(["researcher", "test_analyst"], "coder")
    graph.add_edge("coder", "test_runner")
    graph.add_edge("test_runner", "reviewer")
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
    return graph.compile(checkpointer=checkpointer)
