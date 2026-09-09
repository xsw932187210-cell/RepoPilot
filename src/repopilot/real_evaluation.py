"""Pinned real-defect reproduction and paired real-model evaluation.

The orchestrator is trusted. Repository tests run only in disposable Docker
snapshots, without its Docker socket, model credentials, or benchmark metadata.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any

import docker
from langgraph.checkpoint.memory import InMemorySaver

from repopilot.acceptance import (
    ACCEPTANCE_PROTOCOL_VERSION,
    AcceptanceRunner,
    compare_acceptance,
)
from repopilot.config import Settings
from repopilot.eval_report import build_eval_report, render_markdown
from repopilot.eval_runtime import EvaluationConfig, EvaluationRuntime, ModelCallBudget
from repopilot.github import GitHubPublisher
from repopilot.graph.builder import GraphDependencies, build_graph
from repopilot.llm import build_agent_model
from repopilot.models import FileEdit, PlanOutput
from repopilot.real_tasks import (
    RealTask,
    export_git_tree,
    load_real_task_manifest,
    materialize_hidden_overlay,
)
from repopilot.repository import WorkspaceManager
from repopilot.sandbox import DockerSandbox
from repopilot.security import SecurityError, redact_secrets


def save_json(path: Path, value: object) -> None:
    # Reproduction records are deterministic, replaceable evidence, not model checkpoints.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def image_identity(name: str) -> str:
    client = docker.from_env()
    try:
        return str(client.images.get(name).id)
    finally:
        client.close()


def acceptance_identity(settings: Settings, image_id: str) -> dict[str, object]:
    """Everything outside the image that can alter acceptance behavior."""
    return {
        "protocol_version": ACCEPTANCE_PROTOCOL_VERSION,
        "image_id": image_id,
        "timeout_seconds": settings.sandbox_timeout_seconds,
        "memory": settings.sandbox_memory,
        "pids_limit": 128,
        "network_disabled": True,
        "cap_drop": ["ALL"],
        "no_new_privileges": True,
        "user": "10001:10001",
        "init": True,
    }


def implementation_sha256() -> str:
    """Fingerprint evaluator/orchestration code so changed logic cannot reuse records."""

    root = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        relative = str(path.relative_to(root))
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class OfflineWorkspace(WorkspaceManager):
    """One writer, no Git history; identical issue-only initial retrieval in both arms."""

    def __init__(self, settings: Settings, task: RealTask, source: Path, destination: Path):
        super().__init__(settings)
        self.task, self.source, self.destination = task, source, destination
        self.original: dict[str, str] = {}

    async def prepare(self, task_id: str, repository_url: str, base_branch: str) -> Path:
        del task_id, repository_url, base_branch
        export_git_tree(self.source, self.task.source.buggy_commit, self.destination)
        self.original = {
            str(path.relative_to(self.destination)): path.read_text(encoding="utf-8")
            for path in (self.destination / "thefuck").rglob("*.py")
        }
        return self.destination

    def inspect(self, workspace: Path, issue_text: str, search_terms: list[str]):
        del search_terms
        context = super().inspect(workspace, issue_text, [])
        context.editable_paths = tuple(
            path
            for path in context.files
            if path.startswith("thefuck/") and path.endswith(".py") and path in self.original
        )
        return context

    def apply_edits(self, workspace: Path, edits: list[FileEdit], **kwargs):
        for edit in edits:
            if not edit.path.startswith("thefuck/") or not edit.path.endswith(".py"):
                raise SecurityError(
                    "Real-corpus candidates may edit only existing thefuck Python source: "
                    f"{edit.path}"
                )
            if edit.path not in self.original:
                raise SecurityError("Real-corpus candidate attempted to add an untracked path")
        return super().apply_edits(workspace, edits, **kwargs)

    async def diff(self, workspace: Path) -> str:
        return "".join(
            "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    (workspace / path).read_text(encoding="utf-8").splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
            for path, before in self.original.items()
        )[:100_000]


async def reproduce(
    task: RealTask, source: Path, corpus: Path, settings: Settings
) -> dict[str, Any]:
    evidence = {}
    with tempfile.TemporaryDirectory(prefix="repopilot-reproduce-") as temporary:
        for label, revision in (
            ("buggy", task.source.buggy_commit),
            ("fixed", task.source.fixed_commit),
        ):
            workspace = export_git_tree(source, revision, Path(temporary) / label)
            if label == "buggy":
                evidence["visible"] = await AcceptanceRunner(settings).run(
                    workspace, shlex.join(task.test.visible_argv)
                )
            materialize_hidden_overlay(task, corpus, workspace)
            evidence[label] = await AcceptanceRunner(settings).run(
                workspace, shlex.join(task.test.acceptance_argv)
            )
    comparison = compare_acceptance(
        evidence["buggy"]["outcomes"], evidence["fixed"]["outcomes"], evidence["fixed"]["outcomes"]
    )
    return {
        "case_id": task.id,
        "task_fingerprint": task.fingerprint,
        "verified": evidence["visible"]["exit_code"] == 0
        and evidence["buggy"]["exit_code"] == 1
        and evidence["fixed"]["exit_code"] == 0
        and comparison["resolved"],
        "comparison": comparison,
        **evidence,
    }


async def run_experiment(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_real_task_manifest(args.manifest)
    tasks = list(manifest.tasks)
    if args.case:
        tasks = [manifest.by_id(case_id) for case_id in args.case]
    settings_overrides: dict[str, object] = {}
    if args.model:
        settings_overrides["model_name"] = args.model
    settings = Settings(
        sandbox_backend="docker",
        sandbox_image=args.image,
        github_write_enabled=False,
        sandbox_timeout_seconds=120,
        max_context_files=12,
        max_context_chars=70_000,
        **settings_overrides,
    )
    image_id = image_identity(args.image)
    settings.sandbox_image = image_id  # Resolve the mutable tag before any run starts.
    evaluator_identity = acceptance_identity(settings, image_id)
    corpus = args.manifest.parent
    reproduction_dir = args.output / "reproduction"
    verified: dict[str, dict[str, Any]] = {}
    for task in tasks:
        path = reproduction_dir / f"{task.id}.json"
        existing = json.loads(path.read_text()) if path.exists() else None
        if (
            existing
            and existing.get("verified") is True
            and existing.get("task_fingerprint") == task.fingerprint
            and existing.get("evaluator_identity") == evaluator_identity
        ):
            record = existing
        else:
            try:
                record = {
                    **await reproduce(task, args.source, corpus, settings),
                    "evaluator_identity": evaluator_identity,
                }
            except Exception as error:
                # Do not save provider/transport messages which might include credentials.
                record = {
                    "case_id": task.id,
                    "verified": False,
                    "error_type": type(error).__name__,
                    "error_message": redact_secrets(str(error))[:1_000],
                    "task_fingerprint": task.fingerprint,
                    "evaluator_identity": evaluator_identity,
                }
            save_json(path, record)
        print(
            f"reproduction {task.id}: {'verified' if record['verified'] else 'not verified'}",
            flush=True,
        )
        if record["verified"]:
            verified[task.id] = record
    summary: dict[str, Any] = {
        "selected": len(tasks),
        "reproduction_verified": len(verified),
        "evaluator_identity": evaluator_identity,
    }
    save_json(args.output / "reproduction-summary.json", summary)
    if args.reproduce_only:
        return summary
    if settings.model_provider != "openai":
        raise ValueError("Real benchmark requires MODEL_PROVIDER=openai; mock is not a real result")
    if len(verified) != len(tasks):
        raise ValueError(
            "Every selected task must pass buggy/fixed reproduction before model evaluation"
        )

    config = EvaluationConfig(
        dataset_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        provider=settings.model_provider,
        model=settings.model_name,
        temperature=settings.model_temperature,
        max_model_calls=args.max_calls,
        modes=("oneshot", "workflow"),
        context_budget={"files": 12, "chars": 70_000, "initial_retrieval": "issue-only"},
        test_evaluator="withheld-junit-v1",
        evaluator_config={
            "acceptance": evaluator_identity,
            "implementation_sha256": implementation_sha256(),
            "max_iterations": 2,
            "selected_ids": [task.id for task in tasks],
            "source_policy": "existing-thefuck-python-only",
            "policy_retry_limit": 1,
            "version": 8,
        },
    )
    runtime = EvaluationRuntime(config, args.output / "model-records")

    async def execute(case: dict[str, Any], mode: str, budget: ModelCallBudget) -> dict[str, Any]:
        task = manifest.by_id(case["id"])
        issue_body = task.issue.body
        started = time.perf_counter()
        model = build_agent_model(settings, budget=budget)
        with tempfile.TemporaryDirectory(prefix="repopilot-real-") as temporary:
            root = Path(temporary)
            manager = OfflineWorkspace(settings, task, args.source, root / "agent")
            visible_command = shlex.join(task.test.visible_argv)
            acceptance_command = shlex.join(task.test.acceptance_argv)
            state: dict[str, Any] = {}
            if mode == "workflow":
                graph = build_graph(
                    GraphDependencies(
                        settings=settings,
                        model=model,
                        workspaces=manager,
                        sandbox=DockerSandbox(settings),
                        publisher=GitHubPublisher(settings),
                    ),
                    InMemorySaver(),
                )
                state = await graph.ainvoke(
                    {
                        "task_id": task.id,
                        "repository_url": task.source.repository_url,
                        "base_branch": "benchmark",
                        "issue_title": task.issue.title,
                        "issue_body": issue_body,
                        "test_command": visible_command,
                        "max_iterations": 2,
                        "policy_retry_count": 0,
                    },
                    {"configurable": {"thread_id": task.id}},
                )
                # Stop at HITL. No synthetic approval and no GitHub publication in a benchmark.
            else:
                workspace = await manager.prepare(task.id, task.source.repository_url, "benchmark")
                context = manager.inspect(workspace, f"{task.issue.title}\n{task.issue.body}", [])
                plan = PlanOutput(
                    summary="Generate one minimal candidate", steps=["Fix the described behavior"]
                )
                changes = await model.propose_changes(
                    task.issue.title, task.issue.body, plan, context, []
                )
                manager.apply_edits(workspace, changes.edits, expected_contents=context.files)
            diff = await manager.diff(manager.destination)
            # Replay only allowed source changes into a pristine tree, then add hidden tests.
            evaluation = export_git_tree(args.source, task.source.buggy_commit, root / "acceptance")
            changed_files = []
            for path, before in manager.original.items():
                after = (manager.destination / path).read_text(encoding="utf-8")
                if before != after:
                    (evaluation / path).write_text(after, encoding="utf-8")
                    changed_files.append(path)
            materialize_hidden_overlay(task, corpus, evaluation)
            acceptance = await AcceptanceRunner(settings).run(evaluation, acceptance_command)
            reproduction = verified[task.id]
            comparison = compare_acceptance(
                reproduction["buggy"]["outcomes"],
                reproduction["fixed"]["outcomes"],
                acceptance["outcomes"],
            )
            resolved = (
                bool(diff.strip()) and acceptance["exit_code"] == 0 and comparison["resolved"]
            )
            workflow_interrupted = bool(state.get("__interrupt__"))
            return {
                "case_id": task.id,
                "mode": mode,
                "resolved": resolved,
                "success": resolved,
                "comparison": comparison,
                "acceptance": acceptance,
                "changed_files": changed_files,
                "diff": diff,
                "workflow_status": (
                    "awaiting_approval" if workflow_interrupted else state.get("status")
                ),
                "workflow_gate_passed": workflow_interrupted if mode == "workflow" else None,
                "review": state.get("review") if mode == "workflow" else None,
                "iterations": state.get("iteration", 1),
                "policy_retry_count": state.get("policy_retry_count", 0),
                "node_metrics": state.get("node_metrics", []),
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }

    report = await runtime.run([{"id": task.id} for task in tasks], execute)
    save_json(args.output / "model-report.json", report)
    aggregate = build_eval_report(report)
    save_json(args.output / "evaluation-summary.json", aggregate)
    (args.output / "evaluation-summary.md").write_text(render_markdown(aggregate), encoding="utf-8")
    return {
        **summary,
        "model_report": str(args.output / "model-report.json"),
        "evaluation_summary": str(args.output / "evaluation-summary.json"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("evals/real/manifest.json"))
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Trusted cached upstream checkout; never passed to model",
    )
    parser.add_argument("--output", type=Path, default=Path("reports/real-v1"))
    parser.add_argument("--image", default="repopilot-real-tasks:local")
    parser.add_argument(
        "--model",
        help="Override MODEL_NAME without modifying the credential-bearing .env file",
    )
    parser.add_argument("--case", action="append")
    parser.add_argument("--max-calls", type=int, default=6)
    parser.add_argument("--reproduce-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_experiment(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
