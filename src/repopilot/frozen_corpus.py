"""Evaluator-owned corpus freeze and contamination boundary; never model input."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from repopilot.data_boundary import require_secret_free
from repopilot.real_tasks import (
    RealTask,
    RealTaskManifest,
    load_real_task_manifest,
    task_fingerprint,
)
from repopilot.security import safe_workspace_path

PARTITIONS = frozenset({"development", "holdout"})
EXPOSURES = frozenset({"reference_example", "prompt_tuning", "router_tuning"})


@dataclass(frozen=True)
class FrozenCorpus:
    dataset_id: str
    fingerprint: str
    manifest_path: Path
    manifest: RealTaskManifest
    assignments: tuple[tuple[str, str], ...]
    contamination_sha256: str
    exposed_holdout: tuple[str, ...]

    def select(self, partition: str) -> tuple[RealTask, ...]:
        if partition not in PARTITIONS:
            raise ValueError("Select an explicit development or holdout partition")
        if partition == "holdout" and self.exposed_holdout:
            raise ValueError("Holdout was exposed; reclassify it in a new corpus freeze")
        selected = {task_id for task_id, split in self.assignments if split == partition}
        return tuple(task for task in self.manifest.tasks if task.id in selected)


def _read_json(path: Path) -> dict:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 2_000_000:
        raise ValueError("Corpus metadata must be a bounded regular JSON file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Corpus metadata must be an object")
    require_secret_free(value)
    return value


def _asset(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Corpus asset must have a contained relative path")
    if any(part in {"", ".", ".."} for part in relative.split("/")):
        raise ValueError("Corpus asset must have a contained relative path")
    path = safe_workspace_path(root, relative)
    if not path.is_file() or path.is_symlink():
        raise ValueError("Missing regular corpus asset")
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_frozen_corpus(path: Path) -> FrozenCorpus:
    """Validate bytes, project-disjoint splits, reproduction and exposure records."""
    raw = _read_json(path)
    if raw.get("schema_version") != 1 or raw.get("fingerprint") != task_fingerprint(raw):
        raise ValueError("Unsupported or modified corpus freeze")
    dataset_id = raw.get("dataset_id")
    if (
        not isinstance(dataset_id, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", dataset_id) is None
    ):
        raise ValueError("Corpus freeze needs a portable dataset identity")
    frozen_at = raw.get("frozen_at")
    if (
        raw.get("test_scope") != "focused" or not isinstance(frozen_at, str)
        or datetime.fromisoformat(frozen_at).tzinfo is None
    ):
        raise ValueError("Corpus freeze must declare date and focused test scope")
    root = path.parent.resolve(strict=True)
    assets = raw.get("assets")
    if not isinstance(assets, dict) or not assets:
        raise ValueError("Corpus freeze must bind its assets")
    for relative, expected in assets.items():
        if _digest(_asset(root, relative)) != expected:
            raise ValueError("Frozen corpus asset checksum mismatch")
    for field in ("manifest", "reproduction", "provenance", "exclusions"):
        if not isinstance(raw.get(field), str) or raw[field] not in assets:
            raise ValueError("Freeze metadata is not bound to its asset bytes")
    manifest_path = _asset(root, raw["manifest"])
    manifest = load_real_task_manifest(manifest_path)
    tasks = {task.id: task for task in manifest.tasks}
    groups = raw.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("Corpus freeze needs project groups")
    assignments: dict[str, str] = {}
    project_partitions: dict[str, str] = {}
    group_ids: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("Invalid corpus group")
        group_id, project, partition = group.get("id"), group.get("project"), group.get("partition")
        if not isinstance(group_id, str) or not group_id or group_id in group_ids:
            raise ValueError("Corpus groups need unique identities")
        group_ids.add(group_id)
        if not isinstance(project, str) or partition not in PARTITIONS:
            raise ValueError("Invalid project partition")
        if project in project_partitions and project_partitions[project] != partition:
            raise ValueError("A project may not cross development and holdout")
        project_partitions[project] = partition
        ids = group.get("task_ids")
        if not isinstance(ids, list) or not ids or not group.get("reason"):
            raise ValueError("Corpus group needs cases and a selection reason")
        for task_id in ids:
            if not isinstance(task_id, str) or task_id not in tasks or task_id in assignments:
                raise ValueError("Each corpus task must have exactly one assignment")
            if tasks[task_id].project != project:
                raise ValueError("Corpus task differs from its project group")
            assignments[task_id] = partition
    if set(assignments) != set(tasks) or set(assignments.values()) != PARTITIONS:
        raise ValueError("Freeze must assign every task and include both partitions")

    reproduction = _read_json(_asset(root, raw["reproduction"]))
    records = reproduction.get("cases")
    if not isinstance(records, list) or len(records) != len(tasks):
        raise ValueError("Frozen corpus needs reproduction for every task")
    observed: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Invalid reproduction record")
        task_id = record.get("case_id")
        if not isinstance(task_id, str) or task_id not in tasks or task_id in observed:
            raise ValueError("Invalid reproduction task identity")
        observed.add(task_id)
        task = tasks[task_id]
        if (
            record.get("verified") is not True
            or record.get("task_fingerprint") != task.fingerprint
            or record.get("adapter_fingerprint") != task.adapter.fingerprint
            or record.get("image_id") != task.adapter.runtime.image
            or record.get("visible_exit_code") != 0
            or record.get("buggy_exit_code") != 1
            or record.get("fixed_exit_code") != 0
            or type(record.get("fail_to_pass_count")) is not int
            or record["fail_to_pass_count"] < 1
        ):
            raise ValueError("Reproduction does not match the frozen task/environment")

    ledger_path = _asset(root, raw.get("contamination_ledger"))
    ledger = _read_json(ledger_path)
    if ledger.get("schema_version") != 1 or ledger.get("dataset_id") != dataset_id:
        raise ValueError("Contamination ledger differs from the frozen corpus")
    events = ledger.get("events")
    if not isinstance(events, list):
        raise ValueError("Contamination events must be explicit")
    exposed: set[str] = set()
    for event in events:
        if not isinstance(event, dict) or event.get("kind") not in EXPOSURES | {"curation"}:
            raise ValueError("Unknown contamination event kind")
        ids = event.get("task_ids")
        if (
            not isinstance(ids, list) or not ids or not event.get("recorded_at")
            or not event.get("details") or any(not isinstance(item, str) for item in ids)
            or len(ids) != len(set(ids))
            or any(task_id not in tasks for task_id in ids)
        ):
            raise ValueError("Contamination event needs known cases, date and details")
        if event["kind"] in EXPOSURES:
            exposed.update(task_id for task_id in ids if assignments[task_id] == "holdout")
    return FrozenCorpus(
        dataset_id=dataset_id,
        fingerprint=raw["fingerprint"],
        manifest_path=manifest_path,
        manifest=manifest,
        assignments=tuple(assignments.items()),
        contamination_sha256=_digest(ledger_path),
        exposed_holdout=tuple(sorted(exposed)),
    )
