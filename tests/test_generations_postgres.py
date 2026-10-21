from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
from approval_helpers import decision_request
from generation_helpers import generation_case, pair
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from repopilot.approvals import ApprovalStore
from repopilot.candidates import ArtifactStore, digest_json
from repopilot.config import Settings
from repopilot.db import Database
from repopilot.generations import (
    GenerationConflict,
    GenerationCoordinator,
    GenerationRecord,
    SQLGenerationStore,
)
from repopilot.migrations import HEAD_REVISION, current_database_revisions, ensure_database_schema
from repopilot.models import TaskCreate, TaskStatus
from repopilot.repository import WorkspaceManager
from repopilot.worker import Worker, checkpointer_context

POSTGRES_URL = os.getenv("REPOPILOT_CH05A_POSTGRES_TEST_URL", "")
LEGACY_SOURCE = os.getenv("REPOPILOT_CH05A_LEGACY_SOURCE", "")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="isolated CH-05A services required")


async def database():
    assert make_url(POSTGRES_URL).database == "repopilot_ch05a_test"
    db = Database(POSTGRES_URL)
    await db.setup()
    return db


async def child(phase, task_id, *, parent_id="", root=None, url=None):
    environment = dict(os.environ)
    if root:
        environment["WORKSPACE_ROOT"] = str(root)
    if url:
        environment["DATABASE_URL"] = url
        environment["LANGGRAPH_DATABASE_URL"] = url.replace("+asyncpg", "")
    if phase == "legacy_approval":
        assert await asyncio.to_thread(Path(LEGACY_SOURCE, "repopilot", "worker.py").is_file)
        environment["PYTHONPATH"] = LEGACY_SOURCE
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "tests/probes/ch05a_process.py",
        phase,
        task_id,
        "--parent-id",
        parent_id,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 90)
    assert process.returncode in {0, 88, 89, 90, 91}, (stdout.decode(), stderr.decode())
    return process.returncode


@pytest.mark.parametrize(
    "phase,code,state,generation",
    [
        ("partial", 88, "incomplete", 0),
        ("sealed", 89, "orphan", 0),
        ("committed", 90, "committed", 1),
    ],
)
async def test_hard_exit_reconciles_without_automatically_accepting_staging(
    tmp_path,
    phase,
    code,
    state,
    generation,
):
    db = await database()
    root = tmp_path / "workspaces"
    try:
        coordinator, parent, workspace, context, edits = await generation_case(root, db)
        assert await child(phase, parent.task_id, parent_id=parent.candidate_id, root=root) == code
        # A newly constructed Worker performs its real startup reconciliation.
        assert await child("reconcile", parent.task_id, root=root) == 0
        await db.close()
        db = await database()  # Reopen all persistent state in a different connection pool.
        settings = Settings(_env_file=None, workspace_root=root)
        recovered = GenerationCoordinator(
            coordinator.artifacts, SQLGenerationStore(db), WorkspaceManager(settings)
        )
        current = await recovered.store.current(parent.task_id)
        assert current.generation == generation
        assert pair(recovered.artifacts, current) == (
            (True, "VALUE = 1\n") if generation else (False, "VALUE = 0\n")
        )
        reports = await recovered.recover(parent.task_id)
        assert sorted(item["state"] for item in reports) == sorted(["committed", state])
        assert (workspace / "helper.py").read_text() == "VALUE = 0\n"
        # Explicitly resubmitting the same operation is allowed; recovery alone did
        # not adopt the orphan. A post-commit replay reuses the single DB generation.
        assert await child("success", parent.task_id, parent_id=parent.candidate_id, root=root) == 0
        current = await recovered.store.current(parent.task_id)
        assert current.generation == 1
        async with db.sessions() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(GenerationRecord)
                .where(GenerationRecord.task_id == parent.task_id)
            )
        assert count == 2
        print(f"CH-05A hard_exit={code} stage={state} visible={generation} replay_generation=1")
    finally:
        await db.close()


async def test_database_cas_rejects_concurrent_old_parent(tmp_path):
    db = await database()
    try:
        coordinator, parent, workspace, context, edits = await generation_case(tmp_path / "ws", db)
        candidates = []
        for batch in (edits, edits[:1]):
            with coordinator.artifacts.materialize(parent) as snapshot:
                coordinator.workspaces.apply_edits(snapshot, batch, expected_contents=context)
                candidates.append(await coordinator.artifacts.freeze(
                    snapshot, task_id=parent.task_id, base_sha=parent.base_sha, generation=1,
                    policy_version=parent.policy_version, base_repository=workspace,
                ))
        results = await asyncio.gather(*[
            SQLGenerationStore(db).commit(parent, digest_json(index), candidate)
            for index, candidate in enumerate(candidates)
        ], return_exceptions=True)
        assert sum(isinstance(result, GenerationConflict) for result in results) == 1
        first = await SQLGenerationStore(db).current(parent.task_id)
        assert first in candidates
        # Direct SQL receiver validation cannot be bypassed by a caller skipping
        # the shared-volume file lock, or by a delayed old graph.
        with pytest.raises(GenerationConflict):
            await SQLGenerationStore(db).commit(parent, "f" * 64, first)
        assert await SQLGenerationStore(db).current(parent.task_id) == first
        print("CH-05A competing_sql_commits winners=1 stale_parent=rejected current_generation=1")
    finally:
        await db.close()


async def new_task(db):
    return await db.create_task(
        TaskCreate(
            repository_url="demo://buggy-calculator",
            issue_title="Fix add returning wrong result",
            issue_body="The add function subtracts instead of adding.",
        )
    )


async def test_checkpoint_replay_after_generation_commit_does_not_repeat_model_or_test():
    db = await database()
    try:
        task = await new_task(db)
        assert await child("graph_pause", task.id) == 91
        url = POSTGRES_URL.replace("+asyncpg", "")
        async with AsyncPostgresSaver.from_conn_string(url) as saver:
            checkpoint = await saver.aget_tuple(
                {"configurable": {"thread_id": task.graph_thread_id}}
            )
        assert checkpoint.checkpoint["channel_values"]["candidate"]["generation"] == 0
        assert (await SQLGenerationStore(db).current(task.id)).generation == 1
        assert (await db.get_model_call_metrics(task.id))["reserved_calls"] == 2
        assert await child("graph_resume", task.id) == 0
        view = await ApprovalStore(db).current(task.id)
        assert view.candidate.generation == 1 and view.evidence.result.passed
        saved = await db.get_task(task.id)
        assert saved.status == TaskStatus.AWAITING_APPROVAL
        assert (await db.get_model_call_metrics(task.id))["reserved_calls"] == 3
        assert [m["node"] for m in saved.result["node_metrics"]].count("test_runner") == 1
        print("CH-05A checkpoint_parent=0 sql_head=1 replay_calls=2->3 test_runner=1")
    finally:
        await db.close()


async def test_upgrade_preserves_ch03a_frozen_approval_and_resumes_original_candidate():
    assert make_url(POSTGRES_URL).database == "repopilot_ch05a_test"
    name = f"repopilot_ch05a_migration_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(POSTGRES_URL.replace("+asyncpg", ""))
    url = make_url(POSTGRES_URL).set(database=name).render_as_string(hide_password=False)
    db = Database(url)
    redis = Redis.from_url(Settings(_env_file=None).redis_url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        await ensure_database_schema(url, revision="20261019_0004")
        task = await new_task(db)
        assert await child("legacy_approval", task.id, url=url) == 0
        original = await ApprovalStore(db).current(task.id)
        artifacts = ArtifactStore(Settings(_env_file=None).workspace_root / ".artifacts")
        original_diff = artifacts.read_diff(original.candidate)
        assert (await db.get_model_call_metrics(task.id))["reserved_calls"] == 3
        await db.setup()
        await db.setup()
        assert await current_database_revisions(url) == (HEAD_REVISION,)
        assert await SQLGenerationStore(db).current(task.id) == original.candidate
        restored = await ApprovalStore(db).current(task.id)
        assert restored == original and artifacts.read_diff(restored.candidate) == original_diff
        decision = await ApprovalStore(db).decide(
            task.id, decision_request(restored), actor="operator"
        )
        settings = Settings(
            _env_file=None,
            database_url=url,
            langgraph_database_url=url.replace("+asyncpg", ""),
            github_write_enabled=False,
            model_provider="mock",
        )
        async with checkpointer_context(settings) as saver:
            await Worker(settings, db, redis, saver).handle({"task_id": task.id})
        completed = await db.get_task(task.id)
        assert completed.status == TaskStatus.COMPLETED, completed.error
        assert completed.result["candidate_id"] == original.candidate.candidate_id
        assert completed.result["approval_decision_id"] == decision.decision_id
        assert (await db.get_model_call_metrics(task.id))["reserved_calls"] == 3
        nodes = [m["node"] for m in completed.result["node_metrics"]]
        assert nodes.count("test_runner") == 1
        print(
            "CH-05A upgrade=0004->0005 frozen_approval_equal=true "
            "calls=3->3 original_completed=true"
        )
    finally:
        await redis.aclose()
        await db.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
