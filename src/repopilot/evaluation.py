from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from repopilot.config import Settings
from repopilot.github import GitHubPublisher
from repopilot.graph import GraphDependencies, build_graph
from repopilot.llm import MockAgentModel
from repopilot.repository import WorkspaceManager
from repopilot.sandbox import LocalSandbox


async def find_demo_repository() -> Path:
    candidates = (
        Path.cwd() / "examples" / "buggy_calculator",
        Path(__file__).parents[2] / "examples" / "buggy_calculator",
    )
    for candidate in candidates:
        if await asyncio.to_thread(candidate.is_dir):
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"Demo repository was not found; searched: {searched}")


async def evaluate_case(case: dict[str, Any], repository_root: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="repopilot-eval-") as temp:
        settings = Settings(
            model_provider="mock",
            workspace_root=Path(temp) / "workspaces",
            demo_repository_root=repository_root,
            sandbox_backend="local",
        )
        dependencies = GraphDependencies(
            settings=settings,
            model=MockAgentModel(),
            workspaces=WorkspaceManager(settings),
            sandbox=LocalSandbox(settings),
            publisher=GitHubPublisher(settings),
        )
        graph = build_graph(dependencies, InMemorySaver())
        task_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": task_id}}
        started = time.perf_counter()
        result = await graph.ainvoke(
            {
                "task_id": task_id,
                "repository_url": "demo://buggy-calculator",
                "issue_title": case["issue_title"],
                "issue_body": case["issue_body"],
                "base_branch": "main",
                "test_command": case.get("test_command", "python -m pytest -q"),
                "max_iterations": 2,
                "reviewer_feedback": [],
                "iteration": 0,
            },
            config=config,
        )
        interrupted = bool(result.get("__interrupt__"))
        if interrupted:
            result = await graph.ainvoke(
                Command(resume={"approved": True, "feedback": "evaluation approval"}),
                config=config,
            )
        test_result = result.get("test_result", {})
        return {
            "name": case["name"],
            "completed": result.get("status") == "completed",
            "interrupted_for_approval": interrupted,
            "tests_passed": test_result.get("exit_code") == 0,
            "iterations": result.get("iteration", 0),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }


async def run(dataset: Path) -> dict[str, Any]:
    repository_root = await find_demo_repository()
    dataset_text = await asyncio.to_thread(dataset.read_text, encoding="utf-8")
    cases = [json.loads(line) for line in dataset_text.splitlines() if line.strip()]
    results = [await evaluate_case(case, repository_root) for case in cases]
    total = len(results)
    return {
        "cases": results,
        "summary": {
            "count": total,
            "task_success_rate": sum(item["completed"] for item in results) / total if total else 0,
            "test_pass_rate": sum(item["tests_passed"] for item in results) / total if total else 0,
            "hitl_rate": sum(item["interrupted_for_approval"] for item in results) / total
            if total
            else 0,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("evals/cases.jsonl"))
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.dataset)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
