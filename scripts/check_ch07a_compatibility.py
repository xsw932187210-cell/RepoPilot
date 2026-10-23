#!/usr/bin/env python3
"""Read/export the pinned 20-case cache; never execute upstream Python on the host."""

from __future__ import annotations

import argparse
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

from repopilot.capabilities import real_evaluation_policy
from repopilot.config import Settings
from repopilot.real_evaluation import save_json
from repopilot.real_tasks import load_real_task_manifest, prepare_agent_workspace
from repopilot.repository import WorkspaceManager
from repopilot.security import CapabilityAction


def verify(source: Path, output: Path) -> dict:
    root = Path(__file__).resolve().parents[1]
    corpus = root / "evals" / "real"
    current = load_real_task_manifest(corpus / "manifest.json")
    legacy = load_real_task_manifest(corpus / "manifest-v1.json", allow_legacy_read=True)
    settings = Settings(_env_file=None, max_context_files=12, max_context_chars=70_000)
    old_policy = real_evaluation_policy(max_file_bytes=settings.max_file_bytes)
    cases = []
    with tempfile.TemporaryDirectory(prefix="repopilot-ch07a-compatibility-") as temporary:
        for task in current.tasks:
            if asdict(replace(task, adapter=None)) != asdict(legacy.by_id(task.id)):
                raise ValueError("Historical case metadata differs")
            workspace = prepare_agent_workspace(task, source, Path(temporary) / task.id)
            new_policy = task.adapter.capability_policy(settings.max_file_bytes)
            files = sorted(
                path.relative_to(workspace).as_posix()
                for path in workspace.rglob("*")
                if path.is_file()
            )
            for path in files:
                for action in (CapabilityAction.READ, CapabilityAction.WRITE):
                    if old_policy.permits(action, path) != new_policy.permits(action, path):
                        raise ValueError("Historical path permission differs")
            issue = f"{task.issue.title}\n{task.issue.body}"
            old = WorkspaceManager(settings, capability_policy=old_policy).inspect(
                workspace, issue, []
            )
            new = WorkspaceManager(settings, capability_policy=new_policy).inspect(
                workspace, issue, []
            )
            if asdict(old) != asdict(new):
                raise ValueError("Historical issue-only retrieval differs")
            cases.append(
                {
                    "case_id": task.id,
                    "task_fingerprint": task.fingerprint,
                    "files_checked": len(files),
                    "writable_files": sum(
                        new_policy.permits(CapabilityAction.WRITE, p) for p in files
                    ),
                    "metadata_equal": True,
                    "permissions_equal": True,
                    "retrieval_equal": True,
                }
            )
    result = {"cases_verified": len(cases), "real_model_calls": 0, "cases": cases}
    save_json(output, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.source, args.output)
    print(f"CH-07A metadata/permissions/retrieval identical: {result['cases_verified']}/20")


if __name__ == "__main__":
    main()
