from __future__ import annotations

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


class TaskCancelled(RuntimeError):
    pass


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
    ) -> None:
        if self.events:
            await self.events.publish(
                state["task_id"],
                kind="node_update",
                node=node,
                message=message,
                payload=payload,
            )

    async def ensure_active(self, state: RepoPilotState) -> None:
        if self.queue and await self.queue.is_cancelled(state["task_id"]):
            raise TaskCancelled("Task cancellation was requested")


def build_graph(deps: GraphDependencies, checkpointer: BaseCheckpointSaver):
    async def prepare(state: RepoPilotState) -> dict[str, Any]:
        await deps.ensure_active(state)
        await deps.emit(state, "prepare", "Preparing isolated repository workspace")
        workspace = await deps.workspaces.prepare(
            state["task_id"], state["repository_url"], state["base_branch"]
        )
        return {"workspace": str(workspace), "status": "running", "iteration": 0}

    async def planner(state: RepoPilotState) -> dict[str, Any]:
        await deps.ensure_active(state)
        plan = await deps.model.plan(state["issue_title"], state["issue_body"])
        await deps.emit(
            state,
            "planner",
            "Planner produced a bounded execution plan",
            {"steps": plan.steps, "risks": plan.risk_notes},
        )
        return {"plan": plan.model_dump()}

    async def researcher(state: RepoPilotState) -> dict[str, Any]:
        await deps.ensure_active(state)
        plan = PlanOutput.model_validate(state["plan"])
        context = deps.workspaces.inspect(
            Path(state["workspace"]),
            f"{state['issue_title']}\n{state['issue_body']}",
            plan.search_terms,
        )
        await deps.emit(
            state,
            "researcher",
            f"Researcher selected {len(context.files)} relevant files",
            {"files": list(context.files)},
        )
        return {"research_tree": context.tree, "research_files": context.files}

    async def test_analyst(state: RepoPilotState) -> dict[str, Any]:
        await deps.ensure_active(state)
        command = state["test_command"]
        strategy = f"Run `{command}` in a network-disabled container and require exit code 0."
        await deps.emit(
            state,
            "test_analyst",
            "Test analyst defined deterministic acceptance evidence",
        )
        return {"test_strategy": strategy}

    async def coder(state: RepoPilotState) -> dict[str, Any]:
        await deps.ensure_active(state)
        context = RepositoryContext(
            tree=state.get("research_tree", []), files=state.get("research_files", {})
        )
        plan = PlanOutput.model_validate(state["plan"])
        changes = await deps.model.propose_changes(
            state["issue_title"],
            state["issue_body"],
            plan,
            context,
            state.get("reviewer_feedback", []),
        )
        changed_files = deps.workspaces.apply_edits(Path(state["workspace"]), changes.edits)
        diff = await deps.workspaces.diff(Path(state["workspace"]))
        iteration = state.get("iteration", 0) + 1
        await deps.emit(
            state,
            "coder",
            f"Coder completed iteration {iteration} with {len(changed_files)} file edits",
            {"changed_files": changed_files, "summary": changes.summary},
        )
        return {
            "edits": [edit.model_dump() for edit in changes.edits],
            "changed_files": changed_files,
            "diff": diff,
            "iteration": iteration,
        }

    async def test_runner(state: RepoPilotState) -> dict[str, Any]:
        await deps.ensure_active(state)
        result = await deps.sandbox.run(Path(state["workspace"]), state["test_command"])
        await deps.emit(
            state,
            "test_runner",
            "Sandbox tests passed" if result.passed else "Sandbox tests failed",
            {
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "timed_out": result.timed_out,
            },
        )
        return {"test_result": result.model_dump()}

    async def reviewer(state: RepoPilotState) -> dict[str, Any]:
        await deps.ensure_active(state)
        result = SandboxResult.model_validate(state["test_result"])
        review = await deps.model.review(state["issue_title"], state.get("diff", ""), result)
        await deps.emit(
            state,
            "reviewer",
            "Reviewer approved the candidate change"
            if review.approved
            else "Reviewer requested another bounded iteration",
            {"approved": review.approved, "feedback": review.feedback},
        )
        return {"review": review.model_dump(), "reviewer_feedback": review.feedback}

    def after_review(state: RepoPilotState) -> str:
        review = ReviewOutput.model_validate(state["review"])
        if review.approved:
            return "approval"
        if state.get("iteration", 0) < state.get("max_iterations", 2):
            return "researcher"
        return "failed"

    def approval(state: RepoPilotState) -> dict[str, Any]:
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
        return {
            "human_approved": approved,
            "human_feedback": feedback,
            "status": "approved" if approved else "cancelled",
        }

    def after_approval(state: RepoPilotState) -> str:
        return "finalize" if state.get("human_approved") else "cancelled"

    async def finalize(state: RepoPilotState) -> dict[str, Any]:
        edits = [FileEdit.model_validate(item) for item in state.get("edits", [])]
        result = await deps.publisher.publish(
            task_id=state["task_id"],
            repository_url=state["repository_url"],
            base_branch=state["base_branch"],
            issue_title=state["issue_title"],
            edits=edits,
        )
        await deps.emit(
            state,
            "finalize",
            "Draft pull request created" if result.published else "Verified artifact is ready",
            {
                "pull_request_url": result.pull_request_url,
                "branch": result.branch,
                "publish_reason": result.reason,
            },
        )
        return {"status": "completed", "pull_request_url": result.pull_request_url}

    async def failed(state: RepoPilotState) -> dict[str, Any]:
        await deps.emit(
            state,
            "failed",
            "Task stopped after the bounded retry budget",
            {
                "iteration": state.get("iteration", 0),
                "feedback": state.get("reviewer_feedback", []),
            },
        )
        return {"status": "failed", "error": "Reviewer rejected all bounded iterations"}

    async def cancelled(state: RepoPilotState) -> dict[str, Any]:
        await deps.emit(state, "cancelled", "Human rejected the side-effecting action")
        return {"status": "cancelled", "error": state.get("human_feedback", "")}

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
        {"approval": "approval", "researcher": "researcher", "failed": "failed"},
    )
    graph.add_conditional_edges(
        "approval", after_approval, {"finalize": "finalize", "cancelled": "cancelled"}
    )
    graph.add_edge("finalize", END)
    graph.add_edge("failed", END)
    graph.add_edge("cancelled", END)
    return graph.compile(checkpointer=checkpointer)
