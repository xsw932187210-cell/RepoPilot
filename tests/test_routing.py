from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from repopilot.candidates import bound_resume
from repopilot.config import Settings
from repopilot.github import GitHubPublisher
from repopilot.graph.builder import GraphDependencies, build_graph
from repopilot.llm import BudgetedAgentModel, MockAgentModel
from repopilot.model_calls import ModelCallBudget, RateLimitError
from repopilot.models import CodeChangeOutput, TaskCreate
from repopilot.repository import RepositoryContext, WorkspaceManager
from repopilot.routing import RoutingPolicy, decide_route
from repopilot.sandbox import LocalSandbox


def visible_context(**overrides):
    return RepositoryContext(**{
        "tree": ["calculator.py"], "files": {"calculator.py": "def add(a,b): return a-b"},
        "editable_paths": ("calculator.py",), "selected_chars": 30,
        "evidence": [{"path": "calculator.py", "matched_symbols": ["add"]}], **overrides,
    })


@pytest.mark.parametrize(("strategy", "body", "overrides", "calls", "selected", "reason"), [
    ("auto", "The add function subtracts instead of adding its arguments.", {}, 6,
     "oneshot", "small_bounded_visible_task"),
    ("auto", "Refactor the architecture across the application and fix add.", {}, 6,
     "workflow", "ambiguous_or_broad_issue"),
    ("oneshot", "Refactor the architecture across the application and fix add.", {}, 6,
     "oneshot", "explicit_strategy"),
    ("workflow", "Fix the add function's return value.", {}, 6,
     "workflow", "explicit_strategy"),
    ("auto", "The add function subtracts instead of adding its arguments.",
     {"editable_paths": ()}, 6, "stop", "no_editable_retrieval_hit"),
    ("auto", "The add function subtracts instead of adding its arguments.",
     {"evidence": []}, 6, "stop", "no_editable_retrieval_hit"),
    ("auto", "Refactor the architecture across the application and fix add.", {}, 2,
     "stop", "insufficient_model_budget"),
    ("oneshot", "The add function subtracts instead of adding its arguments.", {}, 0,
     "stop", "insufficient_model_budget"),
])
def test_routes_use_only_explicit_visible_features(
    strategy, body, overrides, calls, selected, reason,
):
    decision = decide_route(strategy, "Fix add", body, visible_context(**overrides),
                            calls, RoutingPolicy())
    assert decision["selected"] == selected
    assert reason in decision["reasons"]
    assert decision == decide_route(strategy, "Fix add", body, visible_context(**overrides),
                                     calls, RoutingPolicy())


class RecordingModel(MockAgentModel):
    def __init__(self, *, bad_first=False, forbidden_first=False, quota=False):
        self.roles = []
        self.bad_first, self.forbidden_first, self.quota = bad_first, forbidden_first, quota

    async def plan(self, title, body):
        self.roles.append("planner")
        return await super().plan(title, body)

    async def propose_changes(self, title, body, plan, context, feedback):
        self.roles.append("coder")
        if self.quota:
            raise RateLimitError("Daily quota exhausted", daily_quota=True)
        if self.bad_first and self.roles.count("coder") == 1:
            return CodeChangeOutput(summary="No candidate", edits=[])
        result = await super().propose_changes(title, body, plan, context, feedback)
        if self.forbidden_first and self.roles.count("coder") == 1:
            result.edits[0].path = "tests/test_calculator.py"
        return result

    async def review(self, title, diff, result):
        self.roles.append("reviewer")
        return await super().review(title, diff, result)


def graph_fixture(tmp_path, *, limit=6, model=None):
    settings = Settings(
        _env_file=None, model_provider="mock", workspace_root=tmp_path / "workspaces",
        demo_repository_root=Path(__file__).parents[1] / "examples/buggy_calculator",
        sandbox_backend="local", model_max_calls=limit,
    )
    inner = model or RecordingModel()
    budget = ModelCallBudget(limit, max_rate_limit_retries=0)
    publisher = GitHubPublisher(settings)
    publisher.publish = AsyncMock(side_effect=AssertionError("HITL must precede publication"))
    graph = build_graph(GraphDependencies(
        settings=settings, model=BudgetedAgentModel(inner, budget),
        workspaces=WorkspaceManager(settings), sandbox=LocalSandbox(settings),
        publisher=publisher,
    ), InMemorySaver())
    state = {
        "task_id": "routing-case", "repository_url": "demo://buggy-calculator",
        "base_branch": "main", "issue_title": "Fix add returning the wrong result",
        "issue_body": "The add function subtracts instead of adding its two arguments.",
        "test_command": "python -m pytest -q", "max_iterations": 2,
    }
    return graph, inner, budget, publisher, state


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["oneshot", "workflow", "auto"])
async def test_all_routes_test_candidate_and_stop_at_bound_human_approval(tmp_path, strategy):
    graph, model, budget, publisher, state = graph_fixture(tmp_path)
    state["execution_strategy"] = strategy
    config = {"configurable": {"thread_id": "all-routes"}}
    result = await graph.ainvoke(state, config)
    assert result["__interrupt__"]
    assert result["test_evidence"]["result"]["exit_code"] == 0
    expected = ["planner", "coder", "reviewer"] if strategy == "workflow" else ["coder"]
    assert model.roles == expected
    assert budget.snapshot()["reserved_calls"] == len(expected)
    assert result["review_performed"] is (strategy == "workflow")
    if strategy != "workflow":
        assert result["review"]["approved"] is False
        assert "not run" in result["review"]["summary"]
    publisher.publish.assert_not_called()
    stopped = await graph.ainvoke(Command(resume=bound_resume(result, approved=False)), config)
    assert stopped["status"] == "cancelled"
    publisher.publish.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("forbidden", [False, True])
async def test_auto_upgrade_shares_budget_and_preserves_policy_boundary(tmp_path, forbidden):
    inner = RecordingModel(bad_first=not forbidden, forbidden_first=forbidden)
    graph, model, budget, publisher, state = graph_fixture(tmp_path, model=inner)
    state["execution_strategy"] = "auto"
    result = await graph.ainvoke(state, {"configurable": {"thread_id": "upgrade"}})
    assert result["__interrupt__"]
    assert model.roles == ["coder", "planner", "coder", "reviewer"]
    assert budget.snapshot()["reserved_calls"] == 4
    assert result["routing"]["upgrades"] == 1
    assert [item["selected"] for item in result["routing_history"]] == ["oneshot", "workflow"]
    assert (result["policy_retry_count"] == 1) is forbidden
    assert result["changed_files"] == ["calculator.py"]
    publisher.publish.assert_not_called()


@pytest.mark.asyncio
async def test_auto_stops_before_upgrade_when_remaining_budget_cannot_fund_workflow(tmp_path):
    graph, model, budget, publisher, state = graph_fixture(
        tmp_path, limit=3, model=RecordingModel(bad_first=True),
    )
    state["execution_strategy"] = "auto"
    result = await graph.ainvoke(state, {"configurable": {"thread_id": "limited"}})
    assert result["status"] == "failed"
    assert result["error"] == "insufficient_upgrade_budget"
    assert model.roles == ["coder"]
    assert budget.snapshot()["reserved_calls"] == 1
    publisher.publish.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_oneshot_does_not_retry_after_empty_candidate(tmp_path):
    graph, model, budget, _, state = graph_fixture(tmp_path, model=RecordingModel(bad_first=True))
    state["execution_strategy"] = "oneshot"
    result = await graph.ainvoke(state, {"configurable": {"thread_id": "one-attempt"}})
    assert result["status"] == "failed"
    assert model.roles == ["coder"]
    assert budget.snapshot()["reserved_calls"] == 1


@pytest.mark.asyncio
async def test_coder_quota_remains_classifiable_for_resume(tmp_path):
    graph, _, budget, _, state = graph_fixture(tmp_path, model=RecordingModel(quota=True))
    state["execution_strategy"] = "oneshot"
    with pytest.raises(RateLimitError):
        await graph.ainvoke(state, {"configurable": {"thread_id": "quota"}})
    assert budget.snapshot()["reserved_calls"] == 1


def test_api_strategy_is_explicit_and_old_requests_default_to_workflow():
    values = dict(repository_url="demo://buggy-calculator", issue_title="Fix add",
                  issue_body="Addition is incorrect")
    assert TaskCreate(**values).execution_strategy == "workflow"
    assert TaskCreate(**values, execution_strategy="auto").execution_strategy == "auto"
    with pytest.raises(ValueError):
        TaskCreate(**values, execution_strategy="unbounded")


def test_route_policy_changes_identity_and_projection_omits_extra_data():
    from repopilot.data_boundary import public_result

    first = RoutingPolicy()
    second = RoutingPolicy(max_editable_files=3)
    assert first.fingerprint != second.fingerprint
    value = public_result({"routing": {
        "selected": "oneshot", "private": "do not expose",
        "features": {"editable_files": 1, "answer": "not visible"},
    }})
    assert value["routing"] == {"selected": "oneshot", "features": {"editable_files": 1}}
