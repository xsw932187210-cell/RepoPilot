"""CH-06C egress probes against disposable PostgreSQL/API/Worker services."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from redis.asyncio import Redis
from sqlalchemy import insert, select
from sqlalchemy.engine import make_url

from repopilot.api import app
from repopilot.config import Settings
from repopilot.db import Database, EventRecord, TaskRecord
from repopilot.events import EventBus
from repopilot.llm import BudgetedAgentModel, MockAgentModel
from repopilot.models import CodeChangeOutput, FileEdit, TaskCreate, TaskStatus
from repopilot.worker import Worker

POSTGRES_URL = os.getenv("REPOPILOT_CH06C_POSTGRES_TEST_URL", "")
API_URL = os.getenv("REPOPILOT_CH06C_API_URL", "")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="isolated CH-06C services required")
TOKEN = "github_pat_" + "z" * 32
OPAQUE = "opaque-provider-body"


def task_data():
    return TaskCreate(
        repository_url="demo://buggy-calculator",
        issue_title="Fix add returning the wrong result",
        issue_body="The add function subtracts instead of adding.",
    )


def checkpoint_url(url):
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def wait_for(client, task_id, statuses):
    for _ in range(600):
        task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
        assert task["status"] not in {"failed", "cancelled"}, task["status"]
        if task["status"] in statuses:
            return task
        await asyncio.sleep(0.1)
    pytest.fail("CH-06C task did not reach the expected state")


async def assert_checkpoints_secret_free(url, thread_id):
    count = 0
    async with AsyncPostgresSaver.from_conn_string(checkpoint_url(url)) as saver:
        await saver.setup()
        async for item in saver.alist({"configurable": {"thread_id": thread_id}}):
            count += 1
            assert TOKEN not in str(item) and OPAQUE not in str(item)
    assert count > 0
    return count


async def test_actual_api_worker_redacts_before_sql_checkpoint_and_resume():
    assert API_URL and make_url(POSTGRES_URL).database == "repopilot_ch06c_test"
    # The real application uses the shared test DB; the other store probes use
    # ch06c_test. This direct connection reads the application's committed rows.
    app_url = make_url(POSTGRES_URL).set(database="repopilot_ch05a_test").render_as_string(
        hide_password=False
    )
    db = Database(app_url)
    try:
        async with httpx.AsyncClient(base_url=API_URL, timeout=20) as client:
            response = await client.post("/api/v1/tasks", json={
                **task_data().model_dump(),
                "issue_body": f"Reproduce add bug.\nAuthorization: {TOKEN}",
            })
            assert response.status_code == 202 and TOKEN not in response.text
            task = await wait_for(client, response.json()["id"], {"awaiting_approval"})
            response = await client.get(f"/api/v1/tasks/{task['id']}/approval")
            assert response.status_code == 200 and TOKEN not in response.text
            approval = response.json()
            decision = await client.post(f"/api/v1/tasks/{task['id']}/approval", json={
                "schema_version": "approval-v2",
                "approval_id": approval["approval_id"],
                "request_version": approval["request_version"],
                "candidate_id": approval["candidate"]["candidate_id"],
                "evidence_digest": approval["evidence"]["evidence_digest"],
                "approved": True, "feedback": f"ship it {TOKEN}",
            })
            assert decision.status_code in {200, 202} and TOKEN not in decision.text
            completed = await wait_for(client, task["id"], {"completed"})
            events = await client.get(f"/api/v1/tasks/{task['id']}/events")
            assert TOKEN not in events.text
            assert "research_files" not in completed["result"] and "diff" not in completed["result"]
            assert (await client.get(
                f"/api/v1/tasks/{task['id']}/candidates/"
                f"{approval['candidate']['candidate_id']}/diff"
            )).status_code == 503
        async with db.sessions() as session:
            row = await session.get(TaskRecord, task["id"])
            rows = (await session.execute(
                select(EventRecord).where(EventRecord.task_id == task["id"])
            )).scalars().all()
            assert TOKEN not in row.issue_body
            assert all(TOKEN not in json.dumps(r.payload) + r.message for r in rows)
        count = await assert_checkpoints_secret_free(app_url, task["graph_thread_id"])
        assert (await db.get_model_call_metrics(task["id"]))["reserved_calls"] == 3
        print(f"CH-06C actual_api_worker=completed checkpoint_count={count} calls=3")
    finally:
        await db.close()


async def test_postgres_new_events_filtered_legacy_reads_leave_rows_unchanged(monkeypatch):
    assert make_url(POSTGRES_URL).database == "repopilot_ch06c_test"
    db = Database(POSTGRES_URL)
    await db.setup()
    try:
        task = await db.create_task(task_data())
        redis = SimpleNamespace(publish=AsyncMock())
        await EventBus(db, redis).publish(
            task.id, kind="node_update", node="planner", message=TOKEN,
            payload={"feedback": [{"Authorization": OPAQUE}],
                     "error_code": "transport_unavailable", "raw": TOKEN},
        )
        async with db.sessions() as session, session.begin():
            await session.execute(insert(EventRecord).values(
                task_id=task.id, kind="task_error", node="legacy",
                message=TOKEN, payload={"headers": {"api_key": OPAQUE}},
                payload_schema_version=0,
            ))
        for key, value in {
            "settings": Settings(_env_file=None), "database": db,
            "events": None, "queue": None, "redis": None,
        }.items():
            monkeypatch.setattr(app.state, key, value, raising=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.get(f"/api/v1/tasks/{task.id}/events")
        assert response.status_code == 200
        assert TOKEN not in response.text and OPAQUE not in response.text
        assert response.json()[0]["payload"]["error_code"] == "transport_unavailable"
        async with db.sessions() as session:
            rows = (await session.execute(select(EventRecord).where(
                EventRecord.task_id == task.id
            ).order_by(EventRecord.id))).scalars().all()
            assert TOKEN not in rows[0].message
            assert rows[1].message == TOKEN and rows[1].payload["headers"]["api_key"] == OPAQUE
        print("CH-06C postgres_event_projection=passed legacy_rows_unchanged=true")
    finally:
        await db.close()


async def test_provider_failure_is_safe_in_postgres_checkpoints(tmp_path, monkeypatch, caplog):
    class FailingModel(MockAgentModel):
        async def plan(self, *args):
            raise RuntimeError(f"{OPAQUE}\nAuthorization: {TOKEN}")

    monkeypatch.setattr("repopilot.worker.build_agent_model",
                        lambda *args, **kwargs: FailingModel())
    assert make_url(POSTGRES_URL).database == "repopilot_ch06c_test"
    db = Database(POSTGRES_URL)
    await db.setup()
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=False)
    try:
        async with AsyncPostgresSaver.from_conn_string(checkpoint_url(POSTGRES_URL)) as saver:
            await saver.setup()
            worker = Worker(Settings(
                _env_file=None, workspace_root=tmp_path / "private",
                demo_repository_root=Path("examples/buggy_calculator"),
            ), db, redis, saver)
            task = await db.create_task(task_data())
            await worker.handle({"task_id": task.id})
        result = await db.get_task(task.id)
        assert result.status is TaskStatus.FAILED and result.error == "execution_failed"
        count = await assert_checkpoints_secret_free(POSTGRES_URL, task.graph_thread_id)
        assert TOKEN not in caplog.text and OPAQUE not in caplog.text
        print(f"CH-06C provider_failure=classified checkpoint_count={count} secrets_absent=true")
    finally:
        await redis.aclose()
        await db.close()


@pytest.mark.parametrize("proposal_field", ["path", "content"])
async def test_sensitive_proposal_never_enters_postgres_checkpoint(
    tmp_path, monkeypatch, proposal_field,
):
    class SensitiveModel(MockAgentModel):
        async def propose_changes(self, title, body, plan, context, feedback):
            return CodeChangeOutput(summary="unsafe proposal", edits=[FileEdit(
                path=(f"/untrusted-host/{TOKEN}/source.py"
                      if proposal_field == "path" else "calculator.py"),
                content=f"API_KEY = '{TOKEN}'\n" if proposal_field == "content" else "value = 1\n",
            )])

    monkeypatch.setattr("repopilot.worker.build_agent_model",
                        lambda settings, call_control: BudgetedAgentModel(
                            SensitiveModel(), call_control,
                        ))
    assert make_url(POSTGRES_URL).database == "repopilot_ch06c_test"
    db = Database(POSTGRES_URL)
    await db.setup()
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=False)
    try:
        async with AsyncPostgresSaver.from_conn_string(checkpoint_url(POSTGRES_URL)) as saver:
            await saver.setup()
            worker = Worker(Settings(
                _env_file=None, workspace_root=tmp_path / "private",
                demo_repository_root=Path("examples/buggy_calculator"),
            ), db, redis, saver)
            task = await db.create_task(task_data())
            await worker.handle({"task_id": task.id})
        result = await db.get_task(task.id)
        assert result.status is TaskStatus.FAILED and result.error == "sensitive_execution_data"
        assert result.result["generation"] == 0 and result.result["policy_retry_count"] == 0
        assert (await db.get_model_call_metrics(task.id))["reserved_calls"] == 2
        count = await assert_checkpoints_secret_free(POSTGRES_URL, task.graph_thread_id)
        print(
            f"CH-06C proposal_secret={proposal_field} "
            f"refused_before_checkpoint=true count={count}"
        )
    finally:
        await redis.aclose()
        await db.close()
