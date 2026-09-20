from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from repopilot.config import Settings
from repopilot.db import Database
from repopilot.events import JobQueue
from repopilot.github import GitHubPublisher
from repopilot.graph import GraphDependencies, build_graph
from repopilot.graph.builder import TaskCancelled
from repopilot.llm import MockAgentModel
from repopilot.models import (
    CodeChangeOutput,
    FileEdit,
    PlanOutput,
    ReviewOutput,
    SandboxResult,
    TaskCreate,
    TaskStatus,
)
from repopilot.repository import RepositoryContext, WorkspaceManager
from repopilot.sandbox import DockerSandbox, LocalSandbox


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
        Command(resume={"approved": True, "feedback": "resume after rebuild"}),
        config=config,
    )
    assert completed["status"] == "completed"
    assert any(metric["node"] == "approval" for metric in completed["node_metrics"])


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
    assert any("Tool policy rejected" in item for item in model.feedback[1])
    workspace = tmp_path / "workspaces" / "policy-retry"
    assert "return a + b" in (workspace / "calculator.py").read_text(encoding="utf-8")
    assert "invalid test rewrite" not in (
        workspace / "tests" / "test_calculator.py"
    ).read_text(encoding="utf-8")


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
