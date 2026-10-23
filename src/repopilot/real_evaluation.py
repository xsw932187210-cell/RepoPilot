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
import os
import shlex
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import docker
from langgraph.checkpoint.memory import InMemorySaver

from repopilot.acceptance import (
    ACCEPTANCE_PROTOCOL_VERSION,
    AcceptanceRunner,
    compare_acceptance,
)
from repopilot.candidates import ArtifactStore, Candidate
from repopilot.config import Settings
from repopilot.data_boundary import require_secret_free, safe_error
from repopilot.eval_repeats import RepeatConfig, RepeatedEvaluationRuntime
from repopilot.eval_report import build_eval_report, render_markdown
from repopilot.eval_runtime import EvaluationConfig, EvaluationRuntime, ModelCallBudget
from repopilot.frozen_corpus import load_frozen_corpus
from repopilot.github import GitHubPublisher
from repopilot.graph.builder import GraphDependencies, build_graph
from repopilot.llm import build_agent_model, model_role_profile
from repopilot.models import FileEdit, PlanOutput
from repopilot.real_tasks import (
    ProjectAdapter,
    RealTask,
    load_real_task_manifest,
    materialize_hidden_overlay,
    prepare_agent_workspace,
    prepare_reference_workspace,
)
from repopilot.repository import WorkspaceManager
from repopilot.sandbox import DockerSandbox
from repopilot.security import (
    CapabilityAction,
    CapabilityError,
    CapabilityReason,
    contains_known_secret,
    safe_workspace_path,
)

EVALUATOR_CONFIG_VERSION = 12


def save_json(path: Path, value: object) -> None:
    # Reproduction records are deterministic, replaceable evidence, not model checkpoints.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def image_identity(name: str) -> str:
    client = docker.from_env()
    try:
        return str(client.images.get(name).id)
    finally:
        client.close()


def acceptance_identity(
    settings: Settings, image_id: str, adapter: ProjectAdapter
) -> dict[str, object]:
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
        "project": adapter.project,
        "adapter_version": adapter.version,
        "adapter_fingerprint": adapter.fingerprint,
        "pythonpath": list(adapter.runtime.container_pythonpath),
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
        if task.adapter is None:
            raise ValueError("Offline execution requires a current trusted project adapter")
        self.adapter = task.adapter
        super().__init__(
            settings,
            capability_policy=self.adapter.capability_policy(settings.max_file_bytes),
        )
        self.task, self.source, self.destination = task, source, destination
        self.original: dict[str, str] = {}

    async def prepare(self, task_id: str, repository_url: str, base_branch: str) -> Path:
        del task_id, repository_url, base_branch
        prepare_agent_workspace(self.task, self.source, self.destination)
        self.original = {}
        for root in self.adapter.source_roots:
            for path in sorted((self.destination / root).rglob("*.py")):
                relative = path.relative_to(self.destination).as_posix()
                if not self.capability_policy.permits(CapabilityAction.WRITE, relative):
                    continue
                _, path = self._capability_path(self.destination, relative, CapabilityAction.WRITE)
                content = path.read_text(encoding="utf-8")
                if not contains_known_secret(content):
                    self.original[relative] = content
        return self.destination

    async def base_commit(self, workspace: Path) -> str:
        return self.task.source.buggy_commit

    def base_repository(self, workspace: Path) -> Path:
        # Keep Git history/metadata outside the model/test workspace. ArtifactStore
        # reads only this pinned commit's tree/blobs from the trusted source checkout.
        return self.source

    def inspect(self, workspace: Path, issue_text: str, search_terms: list[str]):
        del search_terms
        context = super().inspect(workspace, issue_text, [])
        context.editable_paths = tuple(
            path
            for path in context.files
            if path in self.original
            and self.capability_policy.permits(CapabilityAction.WRITE, path)
        )
        return context

    def apply_edits(self, workspace: Path, edits: list[FileEdit], **kwargs):
        for edit in edits:
            normalized, _ = self._capability_path(workspace, edit.path, CapabilityAction.WRITE)
            if normalized.value not in self.original:
                raise CapabilityError(
                    CapabilityReason.WRITE_DENIED,
                    CapabilityAction.WRITE,
                    normalized.value,
                    "real-corpus candidates may edit only exported existing source files",
                    policy_version=self.capability_policy.version,
                )
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
    if task.adapter is None:
        raise ValueError("Reproduction requires a current trusted project adapter")
    runner = AcceptanceRunner(settings, pythonpath=task.adapter.runtime.container_pythonpath)
    evidence = {}
    with tempfile.TemporaryDirectory(prefix="repopilot-reproduce-") as temporary:
        for label, prepare in (
            ("buggy", prepare_agent_workspace),
            ("fixed", prepare_reference_workspace),
        ):
            workspace = prepare(task, source, Path(temporary) / label)
            if label == "buggy":
                evidence["visible"] = await runner.run(
                    workspace, shlex.join(task.test.visible_argv)
                )
            materialize_hidden_overlay(task, corpus, workspace)
            evidence[label] = await runner.run(workspace, shlex.join(task.test.acceptance_argv))
    comparison = compare_acceptance(
        evidence["buggy"]["outcomes"], evidence["fixed"]["outcomes"], evidence["fixed"]["outcomes"]
    )
    return {
        "case_id": task.id,
        "project": task.project,
        "task_fingerprint": task.fingerprint,
        "verified": evidence["visible"]["exit_code"] == 0
        and evidence["buggy"]["exit_code"] == 1
        and evidence["fixed"]["exit_code"] == 0
        and comparison["resolved"],
        "comparison": comparison,
        **evidence,
    }


async def execute_candidate(
    task: RealTask,
    mode: str,
    source: Path,
    corpus: Path,
    settings: Settings,
    reproduction: dict[str, Any],
    model: Any,
) -> dict[str, Any]:
    """Shared generation/replay path; a deterministic model can exercise synthetic fixtures.

    Only issue text, the buggy tree and visible command enter the graph/model. The
    trusted caller retains reference metadata and performs acceptance after generation.
    """
    if task.adapter is None or mode not in {"oneshot", "workflow"}:
        raise ValueError("A current project adapter and supported evaluation mode are required")
    issue_body = task.issue.body
    started = time.perf_counter()
    with (
        tempfile.TemporaryDirectory(prefix="repopilot-real-") as temporary,
        ExitStack() as snapshots,
    ):
        root = Path(temporary)
        settings = settings.model_copy(update={"workspace_root": root / "workspaces"})
        manager = OfflineWorkspace(settings, task, source, root / "agent")
        visible_command = shlex.join(task.test.visible_argv)
        acceptance_command = shlex.join(task.test.acceptance_argv)
        state: dict[str, Any] = {}
        if mode == "workflow":
            graph = build_graph(
                GraphDependencies(
                    settings=settings,
                    model=model,
                    workspaces=manager,
                    sandbox=DockerSandbox(
                        settings, pythonpath=task.adapter.runtime.container_pythonpath
                    ),
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
        candidate_workspace = manager.destination
        if mode == "workflow":
            artifact_store = ArtifactStore(settings.workspace_root / ".artifacts")
            candidate = Candidate.model_validate(state["candidate"])
            candidate_workspace = snapshots.enter_context(artifact_store.materialize(candidate))
            diff = artifact_store.read_diff(candidate).decode("utf-8", errors="replace")[:100_000]
        else:
            diff = await manager.diff(candidate_workspace)
        # Replay only allowed source changes into a pristine tree, then add hidden tests.
        evaluation = prepare_agent_workspace(task, source, root / "acceptance")
        changed_files = []
        for path, before in manager.original.items():
            after = safe_workspace_path(candidate_workspace, path).read_text(encoding="utf-8")
            if before != after:
                safe_workspace_path(evaluation, path).write_text(after, encoding="utf-8")
                changed_files.append(path)
        materialize_hidden_overlay(task, corpus, evaluation)
        acceptance = await AcceptanceRunner(
            settings, pythonpath=task.adapter.runtime.container_pythonpath
        ).run(evaluation, acceptance_command)
        comparison = compare_acceptance(
            reproduction["buggy"]["outcomes"],
            reproduction["fixed"]["outcomes"],
            acceptance["outcomes"],
        )
        resolved = bool(diff.strip()) and acceptance["exit_code"] == 0 and comparison["resolved"]
        workflow_interrupted = bool(state.get("__interrupt__"))
        return {
            "case_id": task.id,
            "project": task.project,
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


def project_arguments(
    values: list[str] | str | Path | None,
    projects: tuple[str, ...],
    *,
    field: str,
    required: bool,
) -> dict[str, str]:
    """A bare value is compatible only with a single selected project."""
    if values is None:
        values = []
    if isinstance(values, (str, Path)):
        values = [str(values)]
    result: dict[str, str] = {}
    for value in values:
        if "=" in value:
            project, value = value.split("=", 1)
        elif len(projects) == 1:
            project = projects[0]
        else:
            raise ValueError(f"Multiple projects require explicit {field} PROJECT=VALUE mappings")
        if project not in projects or not value or project in result:
            raise ValueError(f"Invalid or duplicate {field} project mapping")
        result[project] = value
    if required and set(result) != set(projects):
        raise ValueError(f"Every selected project requires a trusted {field} mapping")
    return result


def protect_experiment_directory(output: Path, identity: dict[str, Any] | None = None) -> None:
    marker = output / "experiment-identity.json"
    if output.exists() and any(output.iterdir()) and not marker.is_file():
        raise ValueError("Legacy evaluation directory is read-only; choose a new output directory")
    if identity is None:
        return
    require_secret_free(identity)
    if marker.exists():
        if json.loads(marker.read_text(encoding="utf-8")) != identity:
            raise ValueError("Evaluation directory identity differs; choose a new output directory")
    else:
        save_json(marker, identity)


async def run_experiment(args: argparse.Namespace) -> dict[str, Any]:
    frozen = load_frozen_corpus(args.freeze) if getattr(args, "freeze", None) else None
    manifest_path = frozen.manifest_path if frozen else args.manifest
    manifest = frozen.manifest if frozen else load_real_task_manifest(manifest_path)
    manifest.require_executable()
    protect_experiment_directory(args.output)
    tasks = list(frozen.select(args.partition)) if frozen else list(manifest.tasks)
    if args.case:
        if len(args.case) != len(set(args.case)):
            raise ValueError("Selected case IDs must be unique")
        allowed = {task.id: task for task in tasks}
        if any(case_id not in allowed for case_id in args.case):
            raise ValueError("Selected case is outside the declared frozen partition")
        tasks = [allowed[case_id] for case_id in args.case]
    run_id = getattr(args, "run_id", None)
    repeats = getattr(args, "repeats", 1)
    if repeats != 1 and not run_id:
        raise ValueError("Repeats require an explicit run ID and new experiment identity")
    if run_id and (not getattr(args, "strategy_version", None)
                   or not getattr(args, "max_total_calls", None)):
        raise ValueError("Repeated runs require strategy version and total API ceiling")
    modes = tuple(getattr(args, "modes", None) or ("oneshot", "workflow"))
    projects = tuple(dict.fromkeys(task.project for task in tasks))
    sources = {
        project: await asyncio.to_thread(Path(value).resolve, strict=True)
        for project, value in project_arguments(
            args.source, projects, field="source", required=True
        ).items()
    }
    image_overrides = project_arguments(args.image, projects, field="image", required=False)
    settings_overrides: dict[str, object] = {}
    if args.model:
        settings_overrides["model_name"] = args.model
    if getattr(args, "max_output_tokens", None) is not None:
        settings_overrides["model_max_output_tokens"] = args.max_output_tokens
    if getattr(args, "temperature", None) is not None:
        settings_overrides["model_temperature"] = args.temperature
    base_settings = Settings(
        sandbox_backend="docker",
        github_write_enabled=False,
        sandbox_timeout_seconds=120,
        max_context_files=12,
        max_context_chars=70_000,
        **settings_overrides,
    )
    settings_by_project: dict[str, Settings] = {}
    identities: dict[str, dict[str, object]] = {}
    for project in projects:
        adapter = manifest.adapter_for(project)
        pinned_image_id = image_identity(adapter.runtime.image)
        if (
            project in image_overrides
            and image_identity(image_overrides[project]) != pinned_image_id
        ):
            raise ValueError("Runtime image override differs from the pinned project environment")
        settings = base_settings.model_copy(update={"sandbox_image": pinned_image_id})
        settings_by_project[project] = settings
        identities[project] = acceptance_identity(settings, pinned_image_id, adapter)
    evaluator_identity = {
        "manifest_schema_version": manifest.schema_version,
        "evaluator_config_version": EVALUATOR_CONFIG_VERSION,
        "projects": identities,
    }
    dataset_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    implementation_digest = implementation_sha256()
    protect_experiment_directory(
        args.output,
        {
            "manifest_sha256": dataset_digest,
            "selected_ids": [task.id for task in tasks],
            "evaluator_identity": evaluator_identity,
            "implementation_sha256": implementation_digest,
            "provider": base_settings.model_provider,
            "model": base_settings.model_name,
            "temperature": base_settings.model_temperature,
            "model_roles": model_role_profile(base_settings),
            "max_calls": args.max_calls,
            "repeat": {
                "run_id": run_id, "repeats": repeats,
                "strategy_version": getattr(args, "strategy_version", None),
                "max_total_calls": getattr(args, "max_total_calls", None), "modes": list(modes),
            },
            "freeze_fingerprint": frozen.fingerprint if frozen else None,
            "contamination_sha256": frozen.contamination_sha256 if frozen else None,
            "partition": args.partition if frozen else None,
        },
    )
    corpus = manifest_path.parent
    reproduction_dir = args.output / "reproduction"
    verified: dict[str, dict[str, Any]] = {}
    for task in tasks:
        identity = identities[task.project]
        path = reproduction_dir / f"{task.id}.json"
        existing = json.loads(path.read_text()) if path.exists() else None
        if (
            existing
            and existing.get("verified") is True
            and existing.get("task_fingerprint") == task.fingerprint
            and existing.get("evaluator_identity") == identity
        ):
            record = existing
        else:
            try:
                record = {
                    **await reproduce(
                        task, sources[task.project], corpus, settings_by_project[task.project]
                    ),
                    "evaluator_identity": identity,
                }
            except Exception as error:
                record = {
                    "case_id": task.id,
                    "project": task.project,
                    "verified": False,
                    "error_type": safe_error(error)["type"],
                    "error_message": safe_error(error)["code"],
                    "task_fingerprint": task.fingerprint,
                    "evaluator_identity": identity,
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
    if any(task.corpus.kind != "real" for task in tasks):
        raise ValueError("Synthetic fixtures are not real-model benchmark cases")
    if base_settings.model_provider not in {"openai", "google_cloud"}:
        raise ValueError(
            "Real benchmark requires MODEL_PROVIDER=openai or google_cloud; "
            "mock is not a real result"
        )
    if len(verified) != len(tasks):
        raise ValueError(
            "Every selected task must pass buggy/fixed reproduction before model evaluation"
        )

    config = EvaluationConfig(
        dataset_sha256=dataset_digest,
        provider=base_settings.model_provider,
        model=base_settings.model_name,
        temperature=base_settings.model_temperature,
        max_model_calls=args.max_calls,
        modes=modes,
        context_budget={"files": 12, "chars": 70_000, "initial_retrieval": "issue-only"},
        test_evaluator="withheld-junit-v2",
        evaluator_config={
            "model_roles": model_role_profile(base_settings),
            "acceptance": evaluator_identity,
            "implementation_sha256": implementation_digest,
            "max_iterations": 2,
            "selected_ids": [task.id for task in tasks],
            "source_policy": "trusted-project-python-source-v1",
            "policy_retry_limit": 1,
            "version": EVALUATOR_CONFIG_VERSION,
            "freeze_fingerprint": frozen.fingerprint if frozen else None,
            "contamination_sha256": frozen.contamination_sha256 if frozen else None,
            "partition": args.partition if frozen else None,
        },
    )
    policy = {
        "max_rate_limit_retries": base_settings.model_max_rate_limit_retries,
        "max_transient_retries": base_settings.model_max_transient_retries,
        "base_backoff_seconds": base_settings.model_retry_base_seconds,
        "max_backoff_seconds": base_settings.model_max_retry_wait_seconds,
        "max_total_backoff_seconds": base_settings.model_max_total_backoff_seconds,
        "request_timeout_seconds": base_settings.model_request_timeout_seconds,
        "max_output_tokens": base_settings.model_max_output_tokens,
        "max_total_tokens": base_settings.model_max_total_tokens or None,
    }
    runtime = (
        RepeatedEvaluationRuntime(
            RepeatConfig(config, run_id, repeats, args.strategy_version, args.max_total_calls),
            args.output / "model-records", **policy,
        ) if run_id else EvaluationRuntime(config, args.output / "model-records", **policy)
    )

    async def execute(case: dict[str, Any], mode: str, budget: ModelCallBudget) -> dict[str, Any]:
        task = manifest.by_id(case["id"])
        settings = settings_by_project[task.project]
        return await execute_candidate(
            task,
            mode,
            sources[task.project],
            corpus,
            settings,
            verified[task.id],
            build_agent_model(settings, budget=budget),
        )

    report = await runtime.run(
        [{"id": task.id, "project": task.project} for task in tasks], execute
    )
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
    parser.add_argument("--freeze", type=Path, help="Select a verified corpus freeze")
    parser.add_argument("--partition", choices=["development", "holdout"], default="development")
    parser.add_argument("--run-id")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--strategy-version")
    parser.add_argument("--max-total-calls", type=int)
    parser.add_argument("--modes", nargs="+", choices=["oneshot", "workflow"])
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--temperature", type=float)
    parser.add_argument(
        "--source",
        action="append",
        required=True,
        help="Trusted PROJECT=PATH checkout; a bare path is allowed for one selected project",
    )
    parser.add_argument("--output", type=Path, default=Path("reports/real-v2"))
    parser.add_argument(
        "--image",
        action="append",
        help="Optional PROJECT=IMAGE alias; must resolve to the manifest's pinned image",
    )
    parser.add_argument("--model", help="Override the model in a new experiment directory")
    parser.add_argument("--case", action="append")
    parser.add_argument("--max-calls", type=int, default=6)
    parser.add_argument("--reproduce-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_experiment(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
