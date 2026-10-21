from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from repopilot.candidates import ArtifactStore, Candidate, bound_resume
from repopilot.config import Settings
from repopilot.db import Database
from repopilot.events import JobQueue
from repopilot.github import GitHubPublisher
from repopilot.graph import GraphDependencies, build_graph
from repopilot.graph.builder import TaskCancelled
from repopilot.llm import BudgetedAgentModel, MockAgentModel
from repopilot.model_calls import ModelCallBudget
from repopilot.models import (
    CodeChangeOutput,
    FileEdit,
    PlanOutput,
    ReviewOutput,
    SandboxResult,
    TaskCreate,
    TaskStatus,
)
from repopilot.policy_feedback import content_sha256, normalized_policy_feedback
from repopilot.repository import RepositoryContext, WorkspaceManager
from repopilot.sandbox import DockerSandbox, LocalSandbox
from repopilot.security import CapabilityAction, CapabilityError, CapabilityReason, SecurityError
from repopilot.worker import result_summary


def graph_input(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "repository_url": "demo://buggy-calculator",
        "issue_title": "Fix add returning the wrong result",
        "issue_body": "The add(a, b) function subtracts b instead of adding it.",
        "base_branch": "main",
        "test_command": "python -m pytest -q",
        "max_iterations": 2,
        "reviewer_feedback": [],
        "iteration": 0,
        "node_metrics": [],
    }


def dependencies(tmp_path: Path, model: MockAgentModel | None = None) -> GraphDependencies:
    project_root = Path(__file__).parents[1]
    settings = Settings(
        workspace_root=tmp_path / "workspaces",
        demo_repository_root=project_root / "examples" / "buggy_calculator",
        sandbox_backend="local",
    )
    return GraphDependencies(
        settings=settings,
        model=model or MockAgentModel(),
        workspaces=WorkspaceManager(settings),
        sandbox=LocalSandbox(settings),
        publisher=GitHubPublisher(settings),
    )


@pytest.mark.asyncio
async def test_checkpoint_can_resume_after_graph_rebuild(tmp_path: Path) -> None:
    saver = InMemorySaver()
    deps = dependencies(tmp_path)
    config = {"configurable": {"thread_id": "restart-recovery"}}

    interrupted = await build_graph(deps, saver).ainvoke(
        graph_input("restart-recovery"), config=config
    )
    assert interrupted["__interrupt__"]

    rebuilt_graph = build_graph(deps, saver)
    completed = await rebuilt_graph.ainvoke(
        Command(resume=bound_resume(interrupted, approved=True, feedback="resume after rebuild")),
        config=config,
    )
    assert completed["status"] == "completed"
    assert any(metric["node"] == "approval" for metric in completed["node_metrics"])


@pytest.mark.parametrize("decision", [{"approved": True}, True, {"approved": "true"}])
async def test_graph_cannot_resume_from_an_unbound_approval(tmp_path: Path, decision) -> None:
    from repopilot.approvals import ApprovalConflict

    deps = dependencies(tmp_path)
    config = {"configurable": {"thread_id": "unbound-approval"}}
    graph = build_graph(deps, InMemorySaver())
    await graph.ainvoke(graph_input("unbound-approval"), config=config)
    with pytest.raises(ApprovalConflict, match="stale_approval_binding"):
        await graph.ainvoke(Command(resume=decision), config=config)


async def test_waiting_approval_uses_original_artifact_after_workspace_changes(
    tmp_path: Path,
) -> None:
    deps = dependencies(tmp_path)
    config = {"configurable": {"thread_id": "frozen-approval"}}
    graph = build_graph(deps, InMemorySaver())
    result = await graph.ainvoke(graph_input("frozen-approval"), config=config)
    (Path(result["workspace"]) / "calculator.py").write_text("# later workspace bytes\n")
    completed = await graph.ainvoke(
        Command(resume=bound_resume(result, approved=True)),
        config=config,
    )
    assert completed["status"] == "completed"
    assert completed["candidate"] == result["candidate"]
    assert completed["test_evidence"] == result["test_evidence"]


async def test_approved_graph_state_cannot_publish_different_edit_bytes(tmp_path: Path) -> None:
    from repopilot.security import SecurityError

    deps = dependencies(tmp_path)
    config = {"configurable": {"thread_id": "modified-publish-intent"}}
    graph = build_graph(deps, InMemorySaver())
    result = await graph.ainvoke(graph_input("modified-publish-intent"), config=config)
    altered = [{**edit, "content": "# different bytes\n"} for edit in result["edits"]]
    await graph.aupdate_state(config, {"edits": altered})
    with pytest.raises(SecurityError, match="differs from the approved artifact"):
        await graph.ainvoke(Command(resume=bound_resume(result, approved=True)), config=config)


class RejectOnceModel(MockAgentModel):
    def __init__(self) -> None:
        self.review_calls = 0
        self.coder_contexts: list[RepositoryContext] = []

    async def propose_changes(
        self,
        issue_title: str,
        issue_body: str,
        plan: PlanOutput,
        context: RepositoryContext,
        reviewer_feedback: list[str],
    ) -> CodeChangeOutput:
        self.coder_contexts.append(context)
        changes = await super().propose_changes(
            issue_title,
            issue_body,
            plan,
            context,
            reviewer_feedback,
        )
        if len(self.coder_contexts) == 1:
            changes.edits[
                0
            ].content += "\n# Marker distinguishes first-pass and retry context sizes.\n"
        return changes

    async def review(
        self,
        issue_title: str,
        diff: str,
        test_result: SandboxResult,
    ) -> ReviewOutput:
        self.review_calls += 1
        if self.review_calls == 1:
            return ReviewOutput(
                approved=False,
                summary="Request one bounded retry.",
                feedback=["Re-check the scoped change."],
                risk_level="low",
            )
        return await super().review(issue_title, diff, test_result)


@pytest.mark.asyncio
async def test_reviewer_risk_is_escalated_without_speculative_rewrite(tmp_path: Path) -> None:
    model = RejectOnceModel()
    result = await build_graph(dependencies(tmp_path, model), InMemorySaver()).ainvoke(
        graph_input("bounded-retry"),
        config={"configurable": {"thread_id": "bounded-retry"}},
    )
    assert result.get("__interrupt__"), result
    assert result["iteration"] == 1
    assert model.review_calls == 1
    assert len(model.coder_contexts) == 1
    assert any("return a - b" in content for content in model.coder_contexts[0].files.values())
    assert result["initial_retrieval"]["evidence"] == model.coder_contexts[0].evidence
    assert result["initial_retrieval"]["selected_chars"] == model.coder_contexts[0].selected_chars
    assert result["retrieval_selected_chars"] == model.coder_contexts[0].selected_chars
    assert result["review"]["approved"] is False
    assert result["reviewer_feedback"] == ["Re-check the scoped change."]
    assert sum(metric["node"] == "reviewer" for metric in result["node_metrics"]) == 1


class ConcreteRiskModel(RejectOnceModel):
    async def review(
        self,
        issue_title: str,
        diff: str,
        test_result: SandboxResult,
    ) -> ReviewOutput:
        self.review_calls += 1
        if self.review_calls == 1:
            return ReviewOutput(
                approved=False,
                summary="A concrete typo is visible in the diff.",
                feedback=["Remove the concrete typo before approval."],
                risk_level="medium",
            )
        return await MockAgentModel.review(self, issue_title, diff, test_result)


@pytest.mark.asyncio
async def test_concrete_reviewer_risk_gets_one_bounded_remediation(tmp_path: Path) -> None:
    model = ConcreteRiskModel()
    result = await build_graph(dependencies(tmp_path, model), InMemorySaver()).ainvoke(
        graph_input("concrete-review-risk"),
        config={"configurable": {"thread_id": "concrete-review-risk"}},
    )

    assert result.get("__interrupt__"), result
    assert result["iteration"] == 2
    assert model.review_calls == 2
    assert len(model.coder_contexts) == 2


class PolicyViolationThenFixModel(MockAgentModel):
    def __init__(self) -> None:
        self.propose_calls = 0
        self.feedback: list[list[str]] = []

    async def propose_changes(
        self,
        issue_title: str,
        issue_body: str,
        plan: PlanOutput,
        context: RepositoryContext,
        reviewer_feedback: list[str],
    ) -> CodeChangeOutput:
        self.propose_calls += 1
        self.feedback.append(list(reviewer_feedback))
        assert any("return a - b" in content for content in context.files.values())
        changes = await super().propose_changes(
            issue_title, issue_body, plan, context, reviewer_feedback
        )
        if self.propose_calls == 1:
            readonly_test = next(path for path in context.files if path.startswith("tests/"))
            assert readonly_test not in (context.editable_paths or ())
            changes.edits.append(
                FileEdit(
                    path=readonly_test,
                    content=f"{context.files[readonly_test]}\n# invalid test rewrite\n",
                    reason="invalid",
                )
            )
        return changes


@pytest.mark.asyncio
async def test_tool_policy_rejection_gets_one_atomic_coder_retry(tmp_path: Path) -> None:
    model = PolicyViolationThenFixModel()
    result = await build_graph(dependencies(tmp_path, model), InMemorySaver()).ainvoke(
        graph_input("policy-retry"),
        config={"configurable": {"thread_id": "policy-retry"}},
    )

    assert result.get("__interrupt__"), result
    assert result["iteration"] == 1
    assert result["policy_retry_count"] == 1
    assert model.propose_calls == 2
    assert any('"reason_code": "write_denied"' in item for item in model.feedback[1])
    assert result["policy_feedback"] is None
    workspace = tmp_path / "workspaces" / "policy-retry"
    with ArtifactStore(workspace.parent / ".artifacts").materialize(
        Candidate.model_validate(result["candidate"])
    ) as snapshot:
        assert "return a + b" in (snapshot / "calculator.py").read_text(encoding="utf-8")
    assert "return a - b" in (workspace / "calculator.py").read_text(encoding="utf-8")
    assert "invalid test rewrite" not in (workspace / "tests" / "test_calculator.py").read_text(
        encoding="utf-8"
    )


class AlwaysViolatesPolicy(MockAgentModel):
    def __init__(self) -> None:
        self.propose_calls = 0

    async def propose_changes(
        self,
        issue_title: str,
        issue_body: str,
        plan: PlanOutput,
        context: RepositoryContext,
        reviewer_feedback: list[str],
    ) -> CodeChangeOutput:
        self.propose_calls += 1
        changes = await super().propose_changes(
            issue_title, issue_body, plan, context, reviewer_feedback
        )
        readonly_test = next(path for path in context.files if path.startswith("tests/"))
        changes.edits.insert(
            0,
            FileEdit(
                path=readonly_test,
                content=context.files[readonly_test] + "\n# forbidden\n",
                expected_content_sha256=content_sha256(context.files[readonly_test]),
            ),
        )
        return changes


@pytest.mark.asyncio
async def test_consecutive_policy_violations_stop_without_partial_write(tmp_path: Path) -> None:
    model = AlwaysViolatesPolicy()
    result = await build_graph(dependencies(tmp_path, model), InMemorySaver()).ainvoke(
        graph_input("repeated-policy-denial"),
        config={"configurable": {"thread_id": "repeated-policy-denial"}},
    )
    assert result["status"] == "failed"
    assert result["error"] == "policy_retry_exhausted"
    assert result["policy_retry_count"] == 1
    assert result["iteration"] == 0
    assert model.propose_calls == 2
    workspace = tmp_path / "workspaces" / "repeated-policy-denial"
    assert "return a - b" in (workspace / "calculator.py").read_text(encoding="utf-8")
    assert "# forbidden" not in (workspace / "tests" / "test_calculator.py").read_text(
        encoding="utf-8"
    )


class ViolationAfterReviewModel(MockAgentModel):
    def __init__(self) -> None:
        self.propose_calls = 0

    async def propose_changes(
        self,
        issue_title: str,
        issue_body: str,
        plan: PlanOutput,
        context: RepositoryContext,
        reviewer_feedback: list[str],
    ) -> CodeChangeOutput:
        self.propose_calls += 1
        changes = await super().propose_changes(
            issue_title, issue_body, plan, context, reviewer_feedback
        )
        if self.propose_calls in {1, 3}:
            readonly_test = next(path for path in context.files if path.startswith("tests/"))
            changes.edits.append(
                FileEdit(
                    path=readonly_test,
                    content=context.files[readonly_test] + "\n# forbidden\n",
                    expected_content_sha256=content_sha256(context.files[readonly_test]),
                )
            )
        return changes

    async def review(self, issue_title: str, diff: str, test_result: SandboxResult) -> ReviewOutput:
        del issue_title, diff, test_result
        return ReviewOutput(
            approved=False,
            summary="Concrete defect requires remediation.",
            feedback=["Inspect the scoped source change again."],
            risk_level="medium",
        )


@pytest.mark.asyncio
async def test_policy_correction_cap_spans_reviewer_iterations(tmp_path: Path) -> None:
    model = ViolationAfterReviewModel()
    result = await build_graph(dependencies(tmp_path, model), InMemorySaver()).ainvoke(
        graph_input("policy-across-iterations"),
        config={"configurable": {"thread_id": "policy-across-iterations"}},
    )
    assert result["status"] == "failed"
    assert result["error"] == "policy_retry_exhausted"
    assert result["iteration"] == 1
    assert result["policy_retry_count"] == 1
    assert model.propose_calls == 3


class MissingDigestModel(MockAgentModel):
    async def propose_changes(
        self,
        issue_title: str,
        issue_body: str,
        plan: PlanOutput,
        context: RepositoryContext,
        reviewer_feedback: list[str],
    ) -> CodeChangeOutput:
        changes = await super().propose_changes(
            issue_title, issue_body, plan, context, reviewer_feedback
        )
        changes.edits[0].expected_content_sha256 = None
        return changes


@pytest.mark.asyncio
async def test_missing_intent_precondition_rejected_before_write(tmp_path: Path) -> None:
    graph = build_graph(dependencies(tmp_path, MissingDigestModel()), InMemorySaver())
    result = await graph.ainvoke(
        graph_input("missing-intent-digest"),
        config={"configurable": {"thread_id": "missing-intent-digest"}},
    )
    assert result["status"] == "failed"
    assert result["policy_feedback"]["reason_code"] == "expected_content_required"
    source = tmp_path / "workspaces" / "missing-intent-digest" / "calculator.py"
    assert "return a - b" in source.read_text(encoding="utf-8")


class MalformedIntentModel(MockAgentModel):
    async def propose_changes(
        self,
        issue_title: str,
        issue_body: str,
        plan: PlanOutput,
        context: RepositoryContext,
        reviewer_feedback: list[str],
    ) -> CodeChangeOutput:
        del issue_title, issue_body, plan, context, reviewer_feedback
        return {"summary": "invalid", "edits": [{"path": "calculator.py", "action": "delete"}]}  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_malformed_intent_stops_with_stable_error(tmp_path: Path) -> None:
    graph = build_graph(dependencies(tmp_path, MalformedIntentModel()), InMemorySaver())
    result = await graph.ainvoke(
        graph_input("malformed-intent"),
        config={"configurable": {"thread_id": "malformed-intent"}},
    )
    assert result["status"] == "failed"
    assert result["error"] == "invalid_modification_intent"
    assert result["iteration"] == 0
    source = tmp_path / "workspaces" / "malformed-intent" / "calculator.py"
    assert "return a - b" in source.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_policy_retry_uses_existing_model_call_budget(tmp_path: Path) -> None:
    model = AlwaysViolatesPolicy()
    budget = ModelCallBudget(2)
    deps = dependencies(tmp_path)
    deps.model = BudgetedAgentModel(model, budget)
    result = await build_graph(deps, InMemorySaver()).ainvoke(
        graph_input("policy-budget"),
        config={"configurable": {"thread_id": "policy-budget"}},
    )
    assert result["status"] == "failed"
    assert result["error"] == "model_call_budget_exhausted"
    assert result["policy_retry_count"] == 1
    assert model.propose_calls == 1
    assert budget.snapshot()["reserved_calls"] == 2  # planner + first coder


@pytest.mark.asyncio
async def test_policy_retry_count_survives_graph_rebuild(tmp_path: Path) -> None:
    model = AlwaysViolatesPolicy()
    saver = InMemorySaver()
    deps = dependencies(tmp_path, model)
    config = {"configurable": {"thread_id": "policy-checkpoint"}}
    graph = build_graph(deps, saver)
    async for update in graph.astream(
        graph_input("policy-checkpoint"), config=config, stream_mode="updates"
    ):
        if "apply_policy" in update:
            break
    checkpoint = await graph.aget_state(config)
    assert checkpoint.values["policy_retry_count"] == 1
    assert checkpoint.values["policy_feedback"]["reason_code"] == "write_denied"
    assert model.propose_calls == 1

    recovered = await build_graph(deps, saver).ainvoke(None, config=config)
    assert recovered["status"] == "failed"
    assert recovered["policy_retry_count"] == 1
    assert model.propose_calls == 2


class StaleAfterRefreshModel(AlwaysViolatesPolicy):
    def __init__(self) -> None:
        super().__init__()
        self.first_digest: str | None = None

    async def propose_changes(
        self,
        issue_title: str,
        issue_body: str,
        plan: PlanOutput,
        context: RepositoryContext,
        reviewer_feedback: list[str],
    ) -> CodeChangeOutput:
        if self.propose_calls == 0:
            self.first_digest = content_sha256(context.files["calculator.py"])
            return await super().propose_changes(
                issue_title, issue_body, plan, context, reviewer_feedback
            )
        self.propose_calls += 1
        changes = await MockAgentModel.propose_changes(
            self, issue_title, issue_body, plan, context, reviewer_feedback
        )
        assert self.first_digest is not None
        changes.edits[0].expected_content_sha256 = self.first_digest
        return changes


class ExternalWriteAfterDenial(WorkspaceManager):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.inspections = 0

    def inspect(
        self, workspace: Path, issue_text: str, search_terms: list[str]
    ) -> RepositoryContext:
        self.inspections += 1
        if self.inspections == 2:
            source = workspace / "calculator.py"
            source.write_text(source.read_text(encoding="utf-8") + "\n# external writer\n")
        return super().inspect(workspace, issue_text, search_terms)


@pytest.mark.asyncio
async def test_external_write_to_context_materialization_stops_without_overwriting_source(
    tmp_path: Path,
) -> None:
    model = StaleAfterRefreshModel()
    deps = dependencies(tmp_path, model)
    deps.workspaces = ExternalWriteAfterDenial(deps.settings)
    with pytest.raises(SecurityError, match="changed during inspection"):
        await build_graph(deps, InMemorySaver()).ainvoke(
            graph_input("stale-policy-context"),
            config={"configurable": {"thread_id": "stale-policy-context"}},
        )
    source = tmp_path / "workspaces" / "stale-policy-context" / "calculator.py"
    assert "# external writer" not in source.read_text(encoding="utf-8")
    assert "return a - b" in source.read_text(encoding="utf-8")


def test_policy_feedback_never_exposes_untrusted_error_path_or_message() -> None:
    context = RepositoryContext(
        tree=["calculator.py"],
        files={"calculator.py": "return a - b\n"},
        editable_paths=("calculator.py",),
        capability_policy_version="workspace-capabilities-v1",
    )
    marker = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
    error = CapabilityError(
        CapabilityReason.INVALID_PATH,
        CapabilityAction.WRITE,
        f"/Users/person/{marker}/calculator.py",
        f"denied at /Users/person/{marker}/calculator.py",
        policy_version="workspace-capabilities-v1",
    )
    feedback = normalized_policy_feedback(error, context)
    assert feedback.reason_code == "invalid_path"
    assert feedback.target_path is None
    assert feedback.allowed_actions == ["write"]
    assert feedback.allowed_paths == ["calculator.py"]
    assert marker not in feedback.model_dump_json()
    assert "/Users/person" not in feedback.model_dump_json()


class HostPathIntentModel(MockAgentModel):
    async def propose_changes(
        self,
        issue_title: str,
        issue_body: str,
        plan: PlanOutput,
        context: RepositoryContext,
        reviewer_feedback: list[str],
    ) -> CodeChangeOutput:
        del issue_title, issue_body, plan, context, reviewer_feedback
        return CodeChangeOutput(
            summary="out-of-scope path",
            edits=[
                FileEdit(
                    path="/Users/person/sk-abcdefghijklmnopqrstuvwxyz1234567890/source.py",
                    content="unsafe\n",
                )
            ],
        )


@pytest.mark.asyncio
async def test_policy_failure_result_summary_excludes_host_path(tmp_path: Path) -> None:
    graph = build_graph(dependencies(tmp_path, HostPathIntentModel()), InMemorySaver())
    result = await graph.ainvoke(
        graph_input("host-path-intent"),
        config={"configurable": {"thread_id": "host-path-intent"}},
    )
    summary = result_summary(result)
    assert summary["status"] == "failed"
    assert summary["policy_feedback"]["reason_code"] == "outside_context"
    assert summary["policy_feedback"]["target_path"] is None
    assert summary["error"] == "policy_retry_exhausted"
    assert "/Users/person" not in str(summary)
    assert "sk-abcdefghijklmnopqrstuvwxyz1234567890" not in str(summary)


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.queue: list[str] = []

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int,
        nx: bool = False,
    ) -> bool | None:
        del ex
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def exists(self, key: str) -> int:
        return int(key in self.values)

    async def lpush(self, name: str, payload: str) -> None:
        del name
        self.queue.insert(0, payload)

    async def brpop(self, name: str, **kwargs: int) -> tuple[str, str] | None:
        del name, kwargs
        if not self.queue:
            return None
        return "queue", self.queue.pop()

    async def eval(self, script: str, keys: int, key: str, token: str) -> int:
        del script, keys
        if self.values.get(key) != token:
            return 0
        del self.values[key]
        return 1


@pytest.mark.asyncio
async def test_job_queue_is_idempotent_and_cancellable() -> None:
    redis = MemoryRedis()
    queue = JobQueue(redis, "jobs")  # type: ignore[arg-type]
    await queue.enqueue("task-1")
    assert await queue.dequeue(wait_seconds=0) == {"task_id": "task-1", "resume": None}
    assert await queue.acquire("task-1", "owner-a") is True
    assert await queue.acquire("task-1", "owner-b") is False
    await queue.release("task-1", "owner-b")
    assert await queue.acquire("task-1", "owner-c") is False
    await queue.release("task-1", "owner-a")
    assert await queue.acquire("task-1", "owner-c") is True
    await queue.request_cancel("task-1")
    assert await queue.is_cancelled("task-1") is True


@pytest.mark.asyncio
async def test_database_state_survives_reopen(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'tasks.db'}"
    first = Database(database_url)
    await first.setup()
    task = await first.create_task(
        TaskCreate(
            repository_url="demo://buggy-calculator",
            issue_title="Durable task state",
            issue_body="Persist the approval boundary.",
        )
    )
    running = await first.transition_task(
        task.id,
        expected_status=TaskStatus.QUEUED,
        expected_version=task.state_version,
        status=TaskStatus.RUNNING,
    )
    await first.transition_task(
        task.id,
        expected_status=TaskStatus.RUNNING,
        expected_version=running.state_version,
        status=TaskStatus.AWAITING_APPROVAL,
    )
    await first.close()

    reopened = Database(database_url)
    restored = await reopened.get_task(task.id)
    await reopened.close()
    assert restored is not None
    assert restored.status == TaskStatus.AWAITING_APPROVAL


@pytest.mark.asyncio
async def test_local_sandbox_converts_timeout_to_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def timed_out(*args: str, **kwargs: object) -> tuple[int, str, str]:
        del args, kwargs
        raise TimeoutError

    monkeypatch.setattr("repopilot.sandbox.run_process", timed_out)
    result = await LocalSandbox(Settings()).run(tmp_path, "python -m pytest -q")
    assert result.exit_code == 124
    assert result.timed_out is True
    assert "timed out" in result.stderr.lower()


class FakeContainer:
    def put_archive(self, path: str, data: object) -> bool:
        del path, data
        return True

    def start(self) -> None:
        pass

    def wait(self, timeout: int) -> dict[str, int]:
        del timeout
        return {"StatusCode": 0}

    def logs(self, *, stdout: bool, stderr: bool) -> bytes:
        del stdout, stderr
        return b""

    def remove(self, *, force: bool) -> None:
        del force


class FakeContainers:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def create(self, **kwargs: object) -> FakeContainer:
        self.kwargs = kwargs
        return FakeContainer()


class FakeDockerClient:
    def __init__(self) -> None:
        self.containers = FakeContainers()
        self.images = self

    def get(self, name: str):
        from types import SimpleNamespace

        return SimpleNamespace(id="sha256:" + "1" * 64)

    def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_docker_sandbox_enforces_runtime_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeDockerClient()
    monkeypatch.setattr("repopilot.sandbox.docker.from_env", lambda **_: client)
    settings = Settings(sandbox_backend="docker", running_in_container=False)
    result = await DockerSandbox(settings).run(tmp_path, "python -m pytest -q")
    assert result.exit_code == 0
    assert client.containers.kwargs["network_disabled"] is True
    assert client.containers.kwargs["cap_drop"] == ["ALL"]
    assert client.containers.kwargs["security_opt"] == ["no-new-privileges"]
    assert client.containers.kwargs["pids_limit"] == 128


@pytest.mark.asyncio
async def test_cancel_flag_is_enforced_at_graph_boundary(tmp_path: Path) -> None:
    class CancelledQueue:
        async def is_cancelled(self, task_id: str) -> bool:
            del task_id
            return True

    deps = dependencies(tmp_path)
    deps.queue = CancelledQueue()  # type: ignore[assignment]
    with pytest.raises(TaskCancelled):
        await deps.ensure_active({"task_id": "cancelled"})
