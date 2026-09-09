#!/usr/bin/env python3
"""Measure issue-only retrieval on the pinned real-defect corpus without model calls."""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
from pathlib import Path

from repopilot.config import Settings
from repopilot.real_tasks import export_git_tree, load_real_task_manifest
from repopilot.repository import WorkspaceManager


def score_selection(selected: list[str], expected: tuple[str, ...]) -> dict[str, object]:
    ranks = [selected.index(path) + 1 for path in expected if path in selected]
    target_count = len(expected)
    return {
        "target_count": target_count,
        "target_hits": len(ranks),
        "target_recall": len(ranks) / target_count if target_count else None,
        "all_targets_hit": bool(target_count) and len(ranks) == target_count,
        "recall_at_3": sum(rank <= 3 for rank in ranks) / target_count if target_count else None,
        "recall_at_5": sum(rank <= 5 for rank in ranks) / target_count if target_count else None,
        "first_relevant_rank": min(ranks) if ranks else None,
        "reciprocal_rank": 1 / min(ranks) if ranks else 0.0,
    }


def evaluate(manifest_path: Path, source: Path) -> dict[str, object]:
    manifest = load_real_task_manifest(manifest_path)
    settings = Settings(
        _env_file=None,
        max_context_files=12,
        max_context_chars=70_000,
    )
    records: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="repopilot-retrieval-") as temporary:
        root = Path(temporary)
        for task in manifest.tasks:
            workspace = export_git_tree(source, task.source.buggy_commit, root / task.id)
            context = WorkspaceManager(settings).inspect(
                workspace,
                f"{task.issue.title}\n{task.issue.body}",
                [],
            )
            selected = list(context.files)
            records.append(
                {
                    "case_id": task.id,
                    "expected_fix_files": list(task.expected_fix_files),
                    "selected_files": selected,
                    "selected_chars": context.selected_chars,
                    "strategy": context.strategy,
                    **score_selection(selected, task.expected_fix_files),
                }
            )

    recalls = [float(record["target_recall"]) for record in records]
    recall_at_3 = [float(record["recall_at_3"]) for record in records]
    recall_at_5 = [float(record["recall_at_5"]) for record in records]
    reciprocal_ranks = [float(record["reciprocal_rank"]) for record in records]
    return {
        "metadata": {
            "dataset": str(manifest_path),
            "task_count": len(records),
            "retrieval_phase": "buggy-commit issue-only before edits",
            "max_context_files": 12,
            "max_context_chars": 70_000,
        },
        "summary": {
            "target_recall": statistics.fmean(recalls),
            "all_target_hit_cases": sum(bool(record["all_targets_hit"]) for record in records),
            "recall_at_3": statistics.fmean(recall_at_3),
            "recall_at_5": statistics.fmean(recall_at_5),
            "mean_reciprocal_rank": statistics.fmean(reciprocal_ranks),
        },
        "cases": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("evals/real/manifest.json"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.manifest, args.source)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
