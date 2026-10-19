from __future__ import annotations

import asyncio
import os
import sys
import uuid

import asyncpg
import httpx
import pytest
from approval_helpers import approval_case, decision_request
from redis.asyncio import Redis
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url

from repopilot.api import app
from repopilot.approvals import ApprovalConflict, ApprovalStore, DecisionRecord
from repopilot.config import Settings
from repopilot.db import Database
from repopilot.events import JobQueue
from repopilot.migrations import HEAD_REVISION, current_database_revisions, ensure_database_schema
from repopilot.models import TaskCreate, TaskStatus

POSTGRES_URL = os.getenv("REPOPILOT_CH03A_POSTGRES_TEST_URL", "")
API_URL = os.getenv("REPOPILOT_CH03A_API_URL", "")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="isolated CH-03A PostgreSQL required")


async def database():
    assert make_url(POSTGRES_URL).database == "repopilot_ch03a_test"
    result = Database(POSTGRES_URL)
    await result.setup()
    return result


async def test_postgres_previous_head_upgrade_preserves_tasks_and_events():
    assert make_url(POSTGRES_URL).database == "repopilot_ch03a_test"
    name = f"repopilot_ch03a_migration_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(POSTGRES_URL.replace("+asyncpg", ""))
    url = make_url(POSTGRES_URL).set(database=name).render_as_string(hide_password=False)
    before = Database(url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        await ensure_database_schema(url, revision="20260921_0003")
        task = await before.create_task(
            TaskCreate(
                repository_url="demo://buggy-calculator",
                issue_title="Previous revision fixture",
                issue_body="Keep the task and its event while installing approval tables.",
            )
        )
        await before.add_event(task.id, kind="upgrade_fixture", node="probe", message="preserve")
        await before.setup()
        await before.setup()
        assert await current_database_revisions(url) == (HEAD_REVISION,)
        assert (await before.get_task(task.id)).state_version == 1
        assert len(await before.list_events(task.id)) == 1
        async with before.engine.connect() as connection:
            tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            version = (await connection.execute(text("SHOW server_version"))).scalar_one()
        assert {"candidates", "test_evidence", "approval_requests", "approval_decisions"} <= tables
        print(f"CH-03A migration=0003->{HEAD_REVISION} postgres={version} rows_preserved=true")
    finally:
        await before.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def test_postgres_opposite_decisions_have_exactly_one_winner(tmp_path):
    db = await database()
    try:
        _, view = await approval_case(db, tmp_path / "workspaces")
        store = ApprovalStore(db)
        results = await asyncio.gather(
            *[
                store.decide(
                    view.task_id, decision_request(view, approved=approved), actor="operator"
                )
                for approved in (True, False)
            ],
            return_exceptions=True,
        )
        winners = [item for item in results if not isinstance(item, BaseException)]
        conflicts = [item for item in results if isinstance(item, ApprovalConflict)]
        assert len(winners) == len(conflicts) == 1
        assert conflicts[0].code == "approval_already_decided"
        winner = winners[0]
        repeated = await store.decide(
            view.task_id,
            decision_request(view, approved=winner.approved),
            actor="operator",
        )
        assert repeated.decision_id == winner.decision_id
        async with db.sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(DecisionRecord).where(
                            DecisionRecord.task_id == view.task_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert (await db.get_task(view.task_id)).state_version == view.request_version + 1
        print("CH-03A concurrent_opposite_decisions winners=1 conflicts=1 decisions=1")
    finally:
        await db.close()


async def test_postgres_decision_survives_hard_process_exit(tmp_path):
    db = await database()
    try:
        _, view = await approval_case(db, tmp_path / "workspaces")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "tests/probes/approval_decide.py",
            view.task_id,
        )
        assert await process.wait() == 87
        await db.close()
        reopened = Database(POSTGRES_URL)
        try:
            restored = await ApprovalStore(reopened).current(view.task_id)
            assert restored.candidate == view.candidate and restored.evidence == view.evidence
            assert restored.decision.approved and restored.decision.dispatch_state == "pending"
            assert (await reopened.get_task(view.task_id)).status is TaskStatus.QUEUED
            print("CH-03A decision_commit_exit=87 pending_intent_recovered=true")
        finally:
            await reopened.close()
    finally:
        await db.close()


async def waiting_task(client):
    created = await client.post(
        "/api/v1/tasks",
        json={
            "repository_url": "demo://buggy-calculator",
            "issue_title": "Fix add returning wrong result",
            "issue_body": "The add function subtracts instead of adding.",
            "test_command": "python -m pytest -q",
        },
    )
    assert created.status_code == 202
    task_id = created.json()["id"]
    for _ in range(120):
        task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
        if task["status"] == "awaiting_approval":
            view = (await client.get(f"/api/v1/tasks/{task_id}/approval")).json()
            return task_id, view
        assert task["status"] not in {"failed", "cancelled"}, task
        await asyncio.sleep(0.25)
    pytest.fail("task never reached approval")


def request_payload(view, approved=True):
    return {
        "schema_version": "approval-v2",
        "approval_id": view["approval_id"],
        "request_version": view["request_version"],
        "candidate_id": view["candidate"]["candidate_id"],
        "evidence_digest": view["evidence"]["evidence_digest"],
        "approved": approved,
        "feedback": "isolated CH-03A probe",
    }


async def test_real_redis_failure_pending_intent_retry_resumes_original_worker(monkeypatch):
    db = await database()
    settings = Settings(_env_file=None)
    dead_redis = Redis(host="redis", port=1, socket_connect_timeout=0.5)
    redis = Redis.from_url(settings.redis_url)

    class MisleadingDelivery:
        async def enqueue(self, task_id, resume=None):
            # Only the database's approved decision may govern graph resumption.
            await JobQueue(redis, settings.queue_name).enqueue(task_id, resume={"approved": False})

    try:
        async with httpx.AsyncClient(base_url=API_URL, timeout=10, trust_env=False) as remote:
            task_id, view = await waiting_task(remote)
            assert view["evidence"]["image_id"].startswith("sha256:")
            before = (await remote.get(f"/api/v1/tasks/{task_id}/metrics")).json()
            for key, value in {
                "settings": settings,
                "database": db,
                "queue": JobQueue(dead_redis, settings.queue_name),
                "events": None,
                "redis": redis,
            }.items():
                monkeypatch.setattr(app.state, key, value, raising=False)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as in_process:
                response = await in_process.post(
                    f"/api/v1/tasks/{task_id}/approval",
                    json=request_payload(view),
                )
                assert response.status_code == 202
                decision = response.json()["decision"]
                assert decision["dispatch_state"] == "pending"
                assert decision["dispatch_error"] == "queue_delivery_failed"
                await db.close()
                reopened = Database(POSTGRES_URL)
                monkeypatch.setattr(app.state, "database", reopened)
                monkeypatch.setattr(app.state, "queue", MisleadingDelivery())
                try:
                    saved = await ApprovalStore(reopened).current(task_id)
                    assert saved.candidate.model_dump(mode="json") == view["candidate"]
                    assert saved.evidence.model_dump(mode="json") == view["evidence"]
                    response = await in_process.post(
                        f"/api/v1/tasks/{task_id}/approval/dispatch",
                        json={"decision_id": decision["decision_id"]},
                    )
                    assert response.status_code == 200
                    for _ in range(120):
                        task = (await remote.get(f"/api/v1/tasks/{task_id}")).json()
                        if task["status"] == "completed":
                            break
                        assert task["status"] not in {"failed", "cancelled"}, task
                        await asyncio.sleep(0.25)
                    assert task["status"] == "completed"
                    after = (await remote.get(f"/api/v1/tasks/{task_id}/metrics")).json()
                    assert before["model_calls_reserved"] == after["model_calls_reserved"] == 3
                    assert after["node_runs"]["test_runner"] == after["node_runs"]["finalize"] == 1
                    saved = await ApprovalStore(reopened).current(task_id)
                    assert saved.decision.approved and saved.decision.dispatch_state == "consumed"
                    assert saved.decision.decision_id == decision["decision_id"]
                    print(
                        "CH-03A redis_connection_failure pending->retry->consumed "
                        "original_task=true"
                    )
                finally:
                    await reopened.close()
    finally:
        await db.close()
        await dead_redis.aclose()
        await redis.aclose()


async def test_corrupt_artifact_rejected_and_human_rejection_cancels_without_finalize():
    awaitable_db = await database()
    await awaitable_db.close()
    async with httpx.AsyncClient(base_url=API_URL, timeout=10, trust_env=False) as client:
        task_id, view = await waiting_task(client)
        artifact = (
            Settings(_env_file=None).workspace_root
            / ".artifacts"
            / view["candidate"]["artifact_digest"]
            / "tree.tar"
        )
        original = artifact.read_bytes()
        offset = original.index(b"return a + b")
        try:
            artifact.write_bytes(
                original[:offset]
                + original[offset:].replace(
                    b"return a + b",
                    b"return a - b",
                    1,
                )
            )
            response = await client.post(
                f"/api/v1/tasks/{task_id}/approval",
                json=request_payload(view),
            )
            assert response.status_code == 503
            current = (await client.get(f"/api/v1/tasks/{task_id}/approval")).json()
            assert current["decision"] is None
        finally:
            artifact.write_bytes(original)
        response = await client.post(
            f"/api/v1/tasks/{task_id}/approval",
            json=request_payload(view, approved=False),
        )
        assert response.status_code == 200
        for _ in range(120):
            task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
            if task["status"] == "cancelled":
                break
            await asyncio.sleep(0.25)
        assert task["status"] == "cancelled"
        assert all(metric["node"] != "finalize" for metric in task["result"]["node_metrics"])
        response = await client.post(
            f"/api/v1/tasks/{task_id}/approval",
            json=request_payload(view),
        )
        assert response.status_code == 409
        print("CH-03A corrupt_artifact=503 human_rejection=cancelled conflicting_repeat=409")
