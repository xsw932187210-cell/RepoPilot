#!/usr/bin/env python3
"""Trusted corpus QA. Upstream code executes only in isolated Docker children."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

from repopilot.config import Settings
from repopilot.frozen_corpus import load_frozen_corpus
from repopilot.real_evaluation import (
    acceptance_identity,
    image_identity,
    project_arguments,
    reproduce,
    save_json,
)
from repopilot.real_tasks import RealTask, prepare_agent_workspace
from repopilot.repository import WorkspaceManager
from repopilot.security import CapabilityAction


def check_workspace(task: RealTask, source: Path, settings: Settings) -> dict:
    """Inspect source as data; no upstream imports, tests, hooks or setup on host."""
    with tempfile.TemporaryDirectory(prefix="repopilot-corpus-boundary-") as temporary:
        workspace = prepare_agent_workspace(task, source, Path(temporary) / "agent")
        policy = task.adapter.capability_policy(settings.max_file_bytes)
        for overlay in task.test.hidden_overlay:
            content = (workspace / overlay.destination).read_bytes()
            if hashlib.sha256(content).hexdigest() != overlay.replaces_sha256:
                raise ValueError("Agent tree contains a different test revision")
            if policy.permits(CapabilityAction.WRITE, overlay.destination):
                raise ValueError("Agent may write hidden-test destination")
        context = WorkspaceManager(settings, capability_policy=policy).inspect(
            workspace, f"{task.issue.title}\n{task.issue.body}", []
        )
        rendered = context.render()
        if task.source.fixed_commit in rendered or task.fingerprint in rendered:
            raise ValueError("Reference metadata leaked into context")
        files = sorted(path for path in workspace.rglob("*") if path.is_file())
        tree_digest = hashlib.sha256()
        for path in files:
            tree_digest.update(path.relative_to(workspace).as_posix().encode())
            tree_digest.update(b"\0")
            tree_digest.update(hashlib.sha256(path.read_bytes()).digest())
        return {
            "files_checked": len(files),
            "tree_sha256": tree_digest.hexdigest(),
            "history_free": not (workspace / ".git").exists(),
            "original_tests_only": True,
            "test_paths_readonly": True,
            "reference_metadata_in_context": False,
            "context_files": len(context.files),
            "context_chars": len(rendered),
        }


async def verify(args: argparse.Namespace) -> dict:
    corpus = await asyncio.to_thread(load_frozen_corpus, args.freeze)
    tasks = (
        corpus.manifest.tasks if args.partition == "all" else corpus.select(args.partition)
    )
    summary = {
        "dataset_id": corpus.dataset_id,
        "freeze_fingerprint": corpus.fingerprint,
        "contamination_sha256": corpus.contamination_sha256,
        "selected": len(tasks),
        "development": len(corpus.select("development")),
        "holdout": len(corpus.select("holdout")),
        "real_model_calls": 0,
    }
    if args.metadata_only:
        return summary
    if args.output is None:
        raise ValueError("Docker verification requires --output for retained evidence")
    sources = project_arguments(
        args.source, tuple(dict.fromkeys(task.project for task in tasks)),
        field="source", required=True,
    )
    cases = []
    for task in tasks:
        image = await asyncio.to_thread(image_identity, task.adapter.runtime.image)
        if image != task.adapter.runtime.image:
            raise ValueError("Docker resolved a different frozen runtime image")
        settings = Settings(
            _env_file=None, model_provider="mock", review_model_provider="",
            sandbox_backend="docker", sandbox_image=image, sandbox_timeout_seconds=120,
            github_write_enabled=False,
        )
        source = await asyncio.to_thread(Path(sources[task.project]).resolve, strict=True)
        boundary = await asyncio.to_thread(check_workspace, task, source, settings)
        result = await reproduce(task, source, corpus.manifest_path.parent, settings)
        result["evaluator_identity"] = acceptance_identity(settings, image, task.adapter)
        result["workspace_boundary"] = boundary
        save_json(args.output / "private" / f"{task.id}.json", result)
        cases.append({
            "case_id": task.id,
            "project": task.project,
            "verified": result["verified"],
            "workspace_boundary": boundary,
            "fail_to_pass_count": result["comparison"]["fail_to_pass_count"],
            "pass_to_pass_count": result["comparison"]["pass_to_pass_count"],
        })
        print(f"{task.id}: {'verified' if result['verified'] else 'FAILED'}", flush=True)
    summary.update(verified=sum(item["verified"] for item in cases), cases=cases)
    save_json(args.output / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path, default=Path("evals/real/freeze-pilot-v1.json"))
    parser.add_argument("--partition", choices=["all", "development", "holdout"], default="all")
    parser.add_argument("--source", action="append", default=[], metavar="PROJECT=PATH")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--metadata-only", action="store_true")
    result = asyncio.run(verify(parser.parse_args()))
    print(json.dumps({key: value for key, value in result.items() if key != "cases"}))
    if result.get("verified", result["selected"]) != result["selected"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
