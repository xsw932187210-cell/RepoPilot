from __future__ import annotations

import asyncio
import json
import os

import pytest
from generation_helpers import generation_case, pair
from sqlalchemy import select

from repopilot.approvals import ApprovalStore, EvidenceRecord
from repopilot.candidates import ArtifactStore, capture_manifest
from repopilot.db import Database
from repopilot.generations import GenerationConflict, GenerationCoordinator, SQLGenerationStore
from repopilot.models import FileEdit, TaskStatus
from repopilot.repository import WorkspaceManager
from repopilot.security import SecurityError
from repopilot.worker import Worker


async def test_second_file_io_failure_never_changes_visible_generation(tmp_path, monkeypatch):
    coordinator, parent, workspace, context, edits = await generation_case(tmp_path / "workspaces")
    original = coordinator.workspaces._write_prepared_file
    writes = []

    def write(root, path, *args):
        writes.append(path)
        if len(writes) == 2:
            raise OSError("injected second-file write failure")
        original(root, path, *args)

    monkeypatch.setattr(coordinator.workspaces, "_write_prepared_file", write)
    with pytest.raises(OSError, match="second-file"):
        await coordinator.apply(parent, edits, expected_contents=context, base_repository=workspace)
    assert writes == ["calculator.py", "helper.py"]
    assert await coordinator.store.current(parent.task_id) == parent
    assert pair(coordinator.artifacts, parent) == (False, "VALUE = 0\n")
    assert capture_manifest(workspace, parent.base_sha) == parent.manifest
    reports = await coordinator.recover(parent.task_id)
    assert sorted(item["state"] for item in reports) == ["committed", "incomplete"]
    stage_root = coordinator._task_root(parent.task_id)
    partials = [path for path in stage_root.glob("*/tree/calculator.py")]
    assert len(partials) == 1 and "return a + b" in partials[0].read_text()


async def test_readers_keep_complete_bound_snapshot_during_pointer_switch(tmp_path):
    coordinator, parent, workspace, context, edits = await generation_case(tmp_path / "workspaces")
    seen = []

    def observe(phase):
        if phase in {"written", "sealed"}:
            seen.append(pair(coordinator.artifacts, parent))

    coordinator.fault = observe
    with coordinator.artifacts.materialize(parent) as old_snapshot:
        candidate = await coordinator.apply(
            parent, edits, expected_contents=context, base_repository=workspace
        )
        assert "return a - b" in (old_snapshot / "calculator.py").read_text()
        assert (old_snapshot / "helper.py").read_text() == "VALUE = 0\n"
    assert seen == [(False, "VALUE = 0\n")] * 2
    assert pair(coordinator.artifacts, await coordinator.store.current(parent.task_id)) == (
        True,
        "VALUE = 1\n",
    )
    assert candidate.generation == 1 and candidate.base_sha == parent.base_sha
    assert capture_manifest(workspace, parent.base_sha) == parent.manifest


async def test_old_parent_and_old_operation_cannot_replace_newer_generation(tmp_path):
    coordinator, parent, workspace, context, edits = await generation_case(tmp_path / "workspaces")
    first = await coordinator.apply(
        parent, edits, expected_contents=context, base_repository=workspace
    )
    # A checkpoint crash replays exactly the same immutable operation.
    assert (
        await coordinator.apply(parent, edits, expected_contents=context, base_repository=workspace)
        == first
    )
    with pytest.raises(GenerationConflict):
        await coordinator.apply(
            parent, edits[:1], expected_contents=context, base_repository=workspace
        )
    next_edit = [FileEdit(path="helper.py", content="VALUE = 2\n", reason="next generation")]
    second = await coordinator.apply(
        first, next_edit, expected_contents={"helper.py": "VALUE = 1\n"}, base_repository=workspace
    )
    with pytest.raises(GenerationConflict):
        await coordinator.apply(parent, edits, expected_contents=context, base_repository=workspace)
    assert await coordinator.store.current(parent.task_id) == second
    assert pair(coordinator.artifacts, first) == (True, "VALUE = 1\n")


@pytest.mark.parametrize("phase", ["materialized", "written", "sealed"])
async def test_external_staging_changes_stop_without_rollback(tmp_path, phase):
    coordinator, parent, workspace, context, edits = await generation_case(tmp_path / "workspaces")
    external_path = None

    def interfere(current):
        nonlocal external_path
        if current == phase:
            external_path = next(coordinator._task_root(parent.task_id).glob("*/tree/helper.py"))
            external_path.write_text("# external newer bytes\n")

    coordinator.fault = interfere
    with pytest.raises(SecurityError, match="changed"):
        await coordinator.apply(parent, edits, expected_contents=context, base_repository=workspace)
    assert await coordinator.store.current(parent.task_id) == parent
    assert external_path.read_text() == "# external newer bytes\n"
    assert (workspace / "helper.py").read_text() == "VALUE = 0\n"


async def test_link_swap_after_preflight_cannot_write_outside_stage(tmp_path, monkeypatch):
    coordinator, parent, workspace, context, edits = await generation_case(tmp_path / "workspaces")
    outside = tmp_path / "user-file.py"
    outside.write_text("# user contents\n")
    original = coordinator.workspaces._write_prepared_file

    def swap(root, path, *args):
        target = root / path
        target.unlink()
        target.symlink_to(outside)
        original(root, path, *args)

    monkeypatch.setattr(coordinator.workspaces, "_write_prepared_file", swap)
    with pytest.raises(OSError):
        await coordinator.apply(parent, edits, expected_contents=context, base_repository=workspace)
    assert outside.read_text() == "# user contents\n"
    assert await coordinator.store.current(parent.task_id) == parent


@pytest.mark.parametrize(
    "path", ["../../escaped.py", "/outside/escaped.py", "tests/test_calculator.py"]
)
async def test_escape_and_readonly_proposals_have_no_model_writes(tmp_path, monkeypatch, path):
    coordinator, parent, workspace, context, edits = await generation_case(tmp_path / "workspaces")
    writes = []
    monkeypatch.setattr(
        coordinator.workspaces, "_write_prepared_file", lambda *args: writes.append(args)
    )
    with pytest.raises(SecurityError):
        await coordinator.apply(
            parent,
            [edits[0], FileEdit(path=path, content="bad", reason="untrusted")],
            expected_contents=context,
            base_repository=workspace,
        )
    assert writes == []
    assert await coordinator.store.current(parent.task_id) == parent
    assert not (tmp_path / "escaped.py").exists()


async def test_internal_writers_are_serialized_and_competing_parent_is_rejected(tmp_path):
    coordinator, parent, workspace, context, edits = await generation_case(tmp_path / "workspaces")
    second = GenerationCoordinator(coordinator.artifacts, coordinator.store, coordinator.workspaces)
    results = await asyncio.gather(
        coordinator.apply(parent, edits, expected_contents=context, base_repository=workspace),
        second.apply(parent, edits[:1], expected_contents=context, base_repository=workspace),
        return_exceptions=True,
    )
    assert sum(isinstance(item, GenerationConflict) for item in results) == 1
    assert (await coordinator.store.current(parent.task_id)).generation == 1


async def test_recovery_retains_orphans_corruption_and_prejournal_directories(tmp_path):
    coordinator, parent, workspace, context, edits = await generation_case(tmp_path / "workspaces")

    def exit_before_cas(phase):
        if phase == "sealed":
            raise RuntimeError("process interruption fixture")

    coordinator.fault = exit_before_cas
    with pytest.raises(RuntimeError):
        await coordinator.apply(parent, edits, expected_contents=context, base_repository=workspace)
    root = coordinator._task_root(parent.task_id)
    (root / "before-journal").mkdir()
    (root / "broken-journal").mkdir()
    (root / "broken-journal" / "journal.json").write_bytes(b"invalid JSON")
    reports = await coordinator.recover(parent.task_id)
    assert sorted(item["state"] for item in reports) == [
        "committed",
        "corrupt",
        "incomplete",
        "orphan",
    ]
    assert await coordinator.store.current(parent.task_id) == parent
    assert len(list(root.glob("*/journal.json"))) == 3
    assert json.loads((root / "recovery.json").read_bytes())["stages"] == reports


async def test_stale_results_and_evidence_cannot_be_committed(tmp_path):
    from approval_helpers import approval_case

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await db.setup()
    try:
        settings, view = await approval_case(db, tmp_path / "workspaces")
        queued = await db.transition_task(
            view.task_id,
            expected_status=TaskStatus.AWAITING_APPROVAL,
            expected_version=view.request_version,
            status=TaskStatus.QUEUED,
        )
        running = await db.transition_task(
            view.task_id,
            expected_status=queued.status,
            expected_version=queued.state_version,
            status=TaskStatus.RUNNING,
        )
        coordinator = GenerationCoordinator(
            ArtifactStore(settings.workspace_root / ".artifacts"),
            SQLGenerationStore(db),
            WorkspaceManager(settings),
        )
        with coordinator.artifacts.materialize(view.candidate) as snapshot:
            before = (snapshot / "calculator.py").read_text()
        await coordinator.apply(
            view.candidate,
            [FileEdit(path="calculator.py", content=before + "\n# next\n")],
            expected_contents={"calculator.py": before},
            base_repository=settings.workspace_root / view.task_id,
        )
        with pytest.raises(GenerationConflict):
            await ApprovalStore(db).record_evidence(view.task_id, view.candidate, view.evidence)
        with pytest.raises(GenerationConflict):
            await ApprovalStore(db).open_request(
                view.task_id,
                expected_version=running.state_version,
                candidate=view.candidate,
                evidence=view.evidence,
                summary={},
            )
        with pytest.raises(GenerationConflict):
            await db.transition_task(
                view.task_id,
                expected_status=running.status,
                expected_version=running.state_version,
                status=TaskStatus.COMPLETED,
                result={"stale": True},
                expected_candidate_id=view.candidate.candidate_id,
            )
        assert (await db.get_task(view.task_id)).status == TaskStatus.RUNNING
        async with db.sessions() as session:
            rows = (await session.execute(select(EvidenceRecord))).scalars().all()
        assert len(rows) == 1  # Original evidence remains immutable.
    finally:
        await db.close()


async def test_worker_startup_classifies_orphan_without_changing_task_or_head(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'startup.db'}")
    await db.setup()
    try:
        coordinator, parent, workspace, context, edits = await generation_case(tmp_path / "ws", db)

        def before_commit(phase):
            if phase == "sealed":
                raise RuntimeError("interrupted before SQL commit")

        coordinator.fault = before_commit
        with pytest.raises(RuntimeError):
            await coordinator.apply(
                parent, edits, expected_contents=context, base_repository=workspace
            )
        before = await db.get_task(parent.task_id)
        # This startup method does not use a queue, a model, or a sandbox.
        worker = Worker(coordinator.workspaces.settings, db, None, None)
        reports = await worker.reconcile_staging()
        assert sorted(item["state"] for item in reports[parent.task_id]) == ["committed", "orphan"]
        assert await db.get_task(parent.task_id) == before
        assert await SQLGenerationStore(db).current(parent.task_id) == parent
        recovery = coordinator._task_root(parent.task_id) / "recovery.json"
        assert json.loads(recovery.read_bytes())["stages"] == reports[parent.task_id]
    finally:
        await db.close()


@pytest.mark.parametrize("corruption", ["array", "operation_type", "phase_type", "fifo"])
async def test_corrupt_journal_types_are_retained_without_blocking_recovery(tmp_path, corruption):
    coordinator, parent, _, _, _ = await generation_case(tmp_path / "ws")
    stage = coordinator._task_root(parent.task_id) / "corrupt-stage"
    stage.mkdir()
    path = stage / "journal.json"
    if corruption == "fifo":
        os.mkfifo(path)
    elif corruption == "array":
        path.write_text("[]")
    else:
        path.write_text(json.dumps({
            "schema_version": "staging-v1", "task_id": parent.task_id,
            "operation_id": [] if corruption == "operation_type" else "f" * 64,
            "phase": [] if corruption == "phase_type" else "writing",
        }))
    reports = await coordinator.recover(parent.task_id)
    assert {item["state"] for item in reports} == {"committed", "corrupt"}
    assert path.exists()
    assert await coordinator.store.current(parent.task_id) == parent
