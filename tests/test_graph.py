from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from repopilot.config import Settings
from repopilot.github import GitHubPublisher
from repopilot.graph import GraphDependencies, build_graph
from repopilot.llm import MockAgentModel
from repopilot.models import CodeChangeOutput, PlanOutput, ReviewOutput, SandboxResult
from repopilot.repository import RepositoryContext, WorkspaceManager
from repopilot.sandbox import LocalSandbox


@pytest.mark.asyncio
async def test_graph_repairs_demo_and_requires_approval(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    settings = Settings(
        model_provider="mock",
        workspace_root=tmp_path / "workspaces",
        demo_repository_root=project_root / "examples" / "buggy_calculator",
        sandbox_backend="local",
        sandbox_timeout_seconds=30,
    )
    deps = GraphDependencies(
        settings=settings,
        model=MockAgentModel(),
        workspaces=WorkspaceManager(settings),
        sandbox=LocalSandbox(settings),
        publisher=GitHubPublisher(settings),
    )
    graph = build_graph(deps, InMemorySaver())
    config = {"configurable": {"thread_id": "graph-e2e"}}
    result = await graph.ainvoke(
        {
            "task_id": "graph-e2e",
            "repository_url": "demo://buggy-calculator",
            "issue_title": "Fix add returning the wrong result",
            "issue_body": "The add function subtracts instead of adding.",
            "base_branch": "main",
            "test_command": "python -m pytest -q",
            "max_iterations": 2,
            "reviewer_feedback": [],
            "iteration": 0,
            "node_metrics": [],
            "status": "queued",
        },
        config=config,
    )
    assert result["test_result"]["exit_code"] == 0, (
        result["test_result"]["stdout"],
        result["test_result"]["stderr"],
    )
    assert result["review"]["approved"] is True, result["review"]
    assert result["retrieval_strategy"] == "hybrid-bm25-symbol-v1"
    assert "calculator.py" in result["research_files"]
    assert result["research_evidence"]
    assert result["__interrupt__"]
    assert {metric["node"] for metric in result["node_metrics"]} >= {
        "prepare",
        "planner",
        "researcher",
        "test_analyst",
        "coder",
        "test_runner",
        "reviewer",
    }

    completed = await graph.ainvoke(
        Command(resume={"approved": True, "feedback": "ship it"}), config=config
    )
    assert completed["status"] == "completed"
    assert completed["pull_request_url"] is None
    fixed = (tmp_path / "workspaces" / "graph-e2e" / "calculator.py").read_text()
    assert "return a + b" in fixed


@pytest.mark.asyncio
async def test_human_can_reject_verified_change(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    settings = Settings(
        workspace_root=tmp_path / "workspaces",
        demo_repository_root=project_root / "examples" / "buggy_calculator",
        sandbox_backend="local",
    )
    deps = GraphDependencies(
        settings=settings,
        model=MockAgentModel(),
        workspaces=WorkspaceManager(settings),
        sandbox=LocalSandbox(settings),
        publisher=GitHubPublisher(settings),
    )
    graph = build_graph(deps, InMemorySaver())
    config = {"configurable": {"thread_id": "reject-e2e"}}
    await graph.ainvoke(
        {
            "task_id": "reject-e2e",
            "repository_url": "demo://buggy-calculator",
            "issue_title": "Fix add returning the wrong result",
            "issue_body": "The add function subtracts instead of adding.",
            "base_branch": "main",
            "test_command": "python -m pytest -q",
            "max_iterations": 1,
            "reviewer_feedback": [],
            "iteration": 0,
            "node_metrics": [],
        },
        config=config,
    )
    rejected = await graph.ainvoke(
        Command(resume={"approved": False, "feedback": "needs manual review"}), config=config
    )
    assert rejected["status"] == "cancelled"


class OverconfidentReviewerModel(MockAgentModel):
    def __init__(self, *, empty_edits: bool = False) -> None:
        self.empty_edits = empty_edits
        self.received_feedback: list[list[str]] = []

    async def propose_changes(
        self,
        issue_title: str,
        issue_body: str,
        plan: PlanOutput,
        context: RepositoryContext,
        reviewer_feedback: list[str],
    ) -> CodeChangeOutput:
        self.received_feedback.append(list(reviewer_feedback))
        if self.empty_edits:
            return CodeChangeOutput(summary="No change is necessary.", edits=[])
        return await super().propose_changes(
            issue_title, issue_body, plan, context, reviewer_feedback
        )

    async def review(
        self, issue_title: str, diff: str, test_result: SandboxResult
    ) -> ReviewOutput:
        return ReviewOutput(approved=True, summary="The model approves regardless of evidence.")


def gate_graph(
    tmp_path: Path,
    model: OverconfidentReviewerModel,
    outcomes: list[SandboxResult],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, AsyncMock, AsyncMock]:
    settings = Settings(
        model_provider="mock",
        workspace_root=tmp_path / "workspaces",
        demo_repository_root=Path(__file__).parents[1] / "examples" / "buggy_calculator",
        sandbox_backend="local",
        github_write_enabled=False,
    )
    deps = GraphDependencies(
        settings=settings,
        model=model,
        workspaces=WorkspaceManager(settings),
        sandbox=LocalSandbox(settings),
        publisher=GitHubPublisher(settings),
    )
    run = AsyncMock(side_effect=outcomes)
    publish = AsyncMock(side_effect=AssertionError("Unapproved artifacts must not be published"))
    monkeypatch.setattr(deps.sandbox, "run", run)
    monkeypatch.setattr(deps.publisher, "publish", publish)
    return build_graph(deps, InMemorySaver()), run, publish


def gate_input() -> dict[str, Any]:
    return {
        "task_id": "deterministic-gate",
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_code", "timed_out", "empty_edits", "expected_feedback"),
    [
        (1, False, False, "exited with code 1"),
        (0, True, False, "timed out"),
        (0, False, True, "no repository diff"),
    ],
)
async def test_deterministic_gates_override_model_approval_and_stop_at_retry_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    timed_out: bool,
    empty_edits: bool,
    expected_feedback: str,
) -> None:
    model = OverconfidentReviewerModel(empty_edits=empty_edits)
    outcome = SandboxResult(
        command=["python", "-m", "pytest", "-q"],
        exit_code=exit_code,
        timed_out=timed_out,
        duration_ms=1,
    )
    graph, run, publish = gate_graph(tmp_path, model, [outcome, outcome], monkeypatch)
    result = await graph.ainvoke(
        gate_input(), config={"configurable": {"thread_id": "deterministic-gate"}}
    )

    assert result["status"] == "failed"
    assert result["iteration"] == 2
    assert not result.get("__interrupt__")
    assert result["review"]["approved"] is False
    assert expected_feedback in " ".join(result["reviewer_feedback"])
    assert expected_feedback in " ".join(model.received_feedback[1])
    assert run.await_count == 2
    assert not {"approval", "finalize"}.intersection(
        metric["node"] for metric in result["node_metrics"]
    )
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_deterministic_gate_allows_retry_after_tests_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = OverconfidentReviewerModel()
    outcomes = [
        SandboxResult(command=["pytest"], exit_code=1, duration_ms=1),
        SandboxResult(command=["pytest"], exit_code=0, duration_ms=1),
    ]
    graph, run, publish = gate_graph(tmp_path, model, outcomes, monkeypatch)
    result = await graph.ainvoke(
        gate_input(), config={"configurable": {"thread_id": "deterministic-gate"}}
    )

    assert result["__interrupt__"]
    assert result["iteration"] == 2
    assert result["review"]["approved"] is True
    assert result["reviewer_feedback"] == []
    assert "exited with code 1" in " ".join(model.received_feedback[1])
    assert run.await_count == 2
    publish.assert_not_awaited()
