from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import tempfile
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from repopilot.config import Settings
from repopilot.github import GitHubPublisher
from repopilot.graph import GraphDependencies, build_graph
from repopilot.llm import build_agent_model
from repopilot.repository import WorkspaceManager
from repopilot.sandbox import LocalSandbox


async def find_benchmark_repository() -> Path:
    candidates = (
        Path.cwd() / "examples" / "benchmark_suite",
        Path(__file__).parents[2] / "examples" / "benchmark_suite",
    )
    for candidate in candidates:
        if await asyncio.to_thread(candidate.is_dir):
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"Benchmark repository was not found; searched: {searched}")


def percentile(values: list[int], quantile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * quantile)
    return ordered[index]


async def evaluate_case(
    case: dict[str, Any],
    repository_root: Path,
    base_settings: Settings,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="repopilot-eval-") as temp:
        settings = Settings(
            model_provider=base_settings.model_provider,
            model_name=base_settings.model_name,
            openai_api_key=base_settings.openai_api_key,
            openai_base_url=base_settings.openai_base_url,
            model_temperature=base_settings.model_temperature,
            workspace_root=Path(temp) / "workspaces",
            demo_repository_root=repository_root,
            sandbox_backend="local",
            sandbox_timeout_seconds=base_settings.sandbox_timeout_seconds,
            github_write_enabled=False,
        )
        dependencies = GraphDependencies(
            settings=settings,
            model=build_agent_model(settings),
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
                "repository_url": "demo://benchmark-suite",
                "issue_title": case["issue_title"],
                "issue_body": case["issue_body"],
                "base_branch": "main",
                "test_command": case.get("test_command", "python -m pytest -q"),
                "max_iterations": int(case.get("max_iterations", 2)),
                "reviewer_feedback": [],
                "iteration": 0,
                "node_metrics": [],
            },
            config=config,
        )
        interrupted = bool(result.get("__interrupt__"))
        if interrupted:
            result = await graph.ainvoke(
                Command(resume={"approved": True, "feedback": "evaluation approval"}),
                config=config,
            )

        expected_files = set(case.get("expected_files", []))
        changed_files = set(result.get("changed_files", []))
        test_result = result.get("test_result", {})
        completed = result.get("status") == "completed"
        tests_passed = test_result.get("exit_code") == 0 and not test_result.get(
            "timed_out", False
        )
        scope_match = changed_files == expected_files
        node_metrics = result.get("node_metrics", [])
        success = completed and tests_passed and scope_match and interrupted
        return {
            "name": case["name"],
            "category": case.get("category", "uncategorized"),
            "difficulty": case.get("difficulty", "unspecified"),
            "success": success,
            "completed": completed,
            "interrupted_for_approval": interrupted,
            "tests_passed": tests_passed,
            "scope_match": scope_match,
            "expected_files": sorted(expected_files),
            "changed_files": sorted(changed_files),
            "iterations": result.get("iteration", 0),
            "duration_ms": int((time.perf_counter() - started) * 1_000),
            "node_time_ms": sum(int(metric.get("duration_ms", 0)) for metric in node_metrics),
            "error": result.get("error"),
        }


def category_summary(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[str(result["category"])].append(result)
    return {
        category: {
            "count": len(items),
            "success_rate": sum(bool(item["success"]) for item in items) / len(items),
        }
        for category, items in sorted(grouped.items())
    }


async def run(
    dataset: Path,
    *,
    provider: str | None = None,
    model_name: str | None = None,
    max_cases: int | None = None,
) -> dict[str, Any]:
    repository_root = await find_benchmark_repository()
    dataset_text = await asyncio.to_thread(dataset.read_text, encoding="utf-8")
    dataset_hash = hashlib.sha256(dataset_text.encode()).hexdigest()[:12]
    cases = [json.loads(line) for line in dataset_text.splitlines() if line.strip()]
    if max_cases is not None:
        cases = cases[:max_cases]

    base_settings = Settings()
    if provider is not None:
        base_settings.model_provider = provider
    if model_name is not None:
        base_settings.model_name = model_name

    results = [
        await evaluate_case(case, repository_root, base_settings) for case in cases
    ]
    total = len(results)
    durations = [int(item["duration_ms"]) for item in results]
    return {
        "metadata": {
            "dataset": str(dataset),
            "dataset_sha256": dataset_hash,
            "provider": base_settings.model_provider,
            "model": (
                "deterministic-mock-v1"
                if base_settings.model_provider == "mock"
                else base_settings.model_name
            ),
            "note": (
                "Mock results are a deterministic workflow regression baseline, not LLM quality."
                if base_settings.model_provider == "mock"
                else "Real-model results depend on the named model and dataset revision."
            ),
        },
        "cases": results,
        "summary": {
            "count": total,
            "successful": sum(bool(item["success"]) for item in results),
            "success_rate": sum(bool(item["success"]) for item in results) / total
            if total
            else 0,
            "task_completion_rate": sum(bool(item["completed"]) for item in results) / total
            if total
            else 0,
            "test_pass_rate": sum(bool(item["tests_passed"]) for item in results) / total
            if total
            else 0,
            "scope_match_rate": sum(bool(item["scope_match"]) for item in results) / total
            if total
            else 0,
            "hitl_rate": sum(bool(item["interrupted_for_approval"]) for item in results)
            / total
            if total
            else 0,
            "mean_iterations": statistics.fmean(
                int(item["iterations"]) for item in results
            )
            if total
            else 0,
            "median_duration_ms": int(statistics.median(durations)) if durations else 0,
            "p95_duration_ms": percentile(durations, 0.95),
            "categories": category_summary(results),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("evals/cases.jsonl"))
    parser.add_argument("--provider", choices=("mock", "openai"))
    parser.add_argument("--model")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fail-on-regression", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(
        run(
            args.dataset,
            provider=args.provider,
            model_name=args.model,
            max_cases=args.max_cases,
        )
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{rendered}\n", encoding="utf-8")
    if args.fail_on_regression and report["summary"]["successful"] != report["summary"]["count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
