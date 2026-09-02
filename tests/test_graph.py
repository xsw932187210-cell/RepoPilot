from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from repopilot.config import Settings
from repopilot.github import GitHubPublisher
from repopilot.graph import GraphDependencies, build_graph
from repopilot.llm import MockAgentModel
from repopilot.repository import WorkspaceManager
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
