"""Finite CH-06A probes against disposable PostgreSQL/Redis/Docker resources."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import uuid
from pathlib import Path

import asyncpg
import docker
import pytest
from docker.errors import NotFound
from langgraph.checkpoint.memory import InMemorySaver
from legacy_tasks import insert_legacy_task
from redis.asyncio import Redis
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from repopilot.config import Settings
from repopilot.db import Database
from repopilot.execution import ExecutionControl, ExecutionStopped
from repopilot.migrations import HEAD_REVISION, current_database_revisions, ensure_database_schema
from repopilot.models import TaskCreate, TaskStatus
from repopilot.repository import run_process
from repopilot.worker import Worker

POSTGRES_URL = os.getenv("REPOPILOT_CH06A_POSTGRES_TEST_URL", "")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="isolated CH-06A services required")


def task_data(**kwargs):
    return TaskCreate(
        repository_url="demo://buggy-calculator",
        issue_title="Fix add returning the wrong result",
        issue_body="The add(a, b) function subtracts b instead of adding it.",
        **kwargs,
    )


@pytest.mark.parametrize("interrupt_migration", [False, True])
async def test_postgres_upgrade_has_no_invented_legacy_stop_or_deadline(
    monkeypatch, interrupt_migration
):
    assert make_url(POSTGRES_URL).database == "repopilot_ch06a_test"
    name = f"repopilot_ch06a_migration_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(POSTGRES_URL.replace("+asyncpg", ""))
    url = make_url(POSTGRES_URL).set(database=name).render_as_string(hide_password=False)
    db = Database(url)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        await ensure_database_schema(url, revision="20261022_0006")
        legacy = await insert_legacy_task(db, task_data())
        async with db.engine.begin() as connection:
            await connection.execute(
                text("UPDATE tasks SET status='cancelled' WHERE id=:id"), {"id": legacy.id}
            )
        await db.add_event(legacy.id, kind="legacy", node="probe", message="retained")
        if interrupt_migration:
            from alembic import op

            original = op.add_column
            calls = [0]

            def fail_third_column(*args, **kwargs):
                calls[0] += 1
                if calls[0] == 3:
                    raise RuntimeError("CH-06A migration interruption")
                return original(*args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(op, "add_column", fail_third_column)
                with pytest.raises(RuntimeError, match="migration interruption"):
                    await db.setup()
            assert await current_database_revisions(url) == ("20261022_0006",)
            async with db.engine.connect() as connection:
                columns = await connection.run_sync(
                    lambda sync: {c["name"] for c in inspect(sync).get_columns("tasks")}
                )
            assert "execution_timeout_seconds" not in columns
        await db.setup()
        await db.setup()
        task = await db.get_task(legacy.id)
        assert await current_database_revisions(url) == (HEAD_REVISION,)
        assert task.status == TaskStatus.CANCELLED
        assert task.stopped_at is task.stop_requested_at is task.execution_timeout_seconds is None
        assert task.execution_resources == task.cleanup_pending == []
        assert len(await db.list_events(task.id)) == 1
        print(
            f"CH-06A migration=0006->0007 interrupted={interrupt_migration} "
            "legacy_confirmation=false"
        )
    finally:
        await db.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def until(observation, predicate, *, seconds=10):
    async with asyncio.timeout(seconds):
        while True:
            value = await observation()
            if predicate(value):
                return value
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("reason", ["cancel_requested", "deadline_exceeded"])
async def test_real_worker_stops_only_its_test_container_before_confirmation(tmp_path, reason):
    assert make_url(POSTGRES_URL).database == "repopilot_ch06a_test"
    db = Database(POSTGRES_URL)
    await db.setup()
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=False)
    settings = Settings(_env_file=None)
    settings.database_url = POSTGRES_URL
    settings.workspace_root = tmp_path / "workspaces"
    settings.demo_repository_root = tmp_path / "slow-demo"
    settings.cancellation_poll_seconds = 0.02
    shutil.copytree(Path("examples/buggy_calculator"), settings.demo_repository_root)
    (settings.demo_repository_root / "test_slow.py").write_text(
        "import time\ndef test_slow():\n    time.sleep(30)\n"
    )
    client = docker.from_env(timeout=10)
    sibling = await asyncio.to_thread(
        client.containers.run,
        settings.sandbox_image,
        ["python", "-c", "import time; time.sleep(30)"],
        detach=True,
        network_disabled=True,
        labels={"repopilot.probe": "ch06a-unrelated"},
    )
    operation, container_id = None, None
    task = await db.create_task(
        task_data(execution_timeout_seconds=8 if reason == "deadline_exceeded" else 60)
    )
    try:
        worker = Worker(settings, db, redis, InMemorySaver())
        operation = asyncio.create_task(worker.handle({"task_id": task.id}))
        live = await until(
            lambda: db.get_task(task.id),
            lambda t: any(r["kind"] == "docker_container" for r in t.execution_resources),
        )
        container_id = next(
            r["id"] for r in live.execution_resources if r["kind"] == "docker_container"
        )
        container = await asyncio.to_thread(client.containers.get, container_id)
        assert container.labels["repopilot.task_id"] == task.id
        if reason == "cancel_requested":
            requested = await db.request_stop(task.id)
            assert requested.status == TaskStatus.CANCELLING and requested.stopped_at is None
        await asyncio.wait_for(operation, 15)
        stopped = await db.get_task(task.id)
        assert stopped.status == (
            TaskStatus.CANCELLED if reason == "cancel_requested" else TaskStatus.FAILED
        )
        assert stopped.stop_reason == reason and stopped.stopped_at
        assert stopped.execution_resources == stopped.cleanup_pending == []
        with pytest.raises(NotFound):
            await asyncio.to_thread(client.containers.get, container_id)
        await asyncio.to_thread(sibling.reload)
        assert sibling.status == "running"
        metrics = await db.get_model_call_metrics(task.id)
        assert metrics["started_calls"] == 2  # Planner and Coder; no Reviewer after stop.
        events = await db.list_events(task.id)
        assert any(e.kind == "execution_stopped" for e in events)
        assert not any(e.node == "reviewer" for e in events)
        print(
            f"CH-06A real_docker reason={reason} confirmed=true removed_id={container_id} "
            "sibling_running=true model_calls=2"
        )
    finally:
        if operation and not operation.done():
            await db.request_stop(task.id)
            try:
                await asyncio.wait_for(operation, 15)
            except (TimeoutError, asyncio.CancelledError):
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
        if container_id:
            try:
                owned = await asyncio.to_thread(client.containers.get, container_id)
                assert owned.labels["repopilot.task_id"] == task.id
                await asyncio.to_thread(owned.remove, force=True)
            except NotFound:
                pass
        await asyncio.to_thread(sibling.remove, force=True)
        client.close()
        await redis.aclose()
        await db.close()


async def test_real_process_group_cancel_stops_child_and_leaves_other_group(tmp_path):
    marker = tmp_path / "pids"
    source = (
        "import subprocess,sys,os,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "open(sys.argv[1],'w').write(str(os.getpid())+' '+str(p.pid)); time.sleep(30)"
    )
    sibling = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)", start_new_session=True
    )
    control = ExecutionControl("process-probe", 20)
    operation = None
    try:
        with control.bind():
            operation = asyncio.create_task(
                control.run(run_process(sys.executable, "-c", source, str(marker)))
            )

            async def pids():
                return await asyncio.to_thread(
                    lambda: marker.read_text().split() if marker.exists() else []
                )

            ids = await until(pids, lambda value: len(value) == 2, seconds=5)
            control.stop()
            with pytest.raises(ExecutionStopped):
                await asyncio.wait_for(operation, 5)
        for pid in ids:
            status = Path(f"/proc/{pid}/stat")
            assert await asyncio.to_thread(
                lambda status=status: not status.exists() or status.read_text().split()[2] == "Z"
            )
        assert sibling.returncode is None and control.resources == []
        print("CH-06A real_process parent_stopped=true child_stopped=true sibling_running=true")
    finally:
        if operation and not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        sibling.kill()
        await sibling.wait()


@pytest.mark.parametrize("stage", ["blob", "pr"])
async def test_postgres_inflight_publication_cancellation_keeps_receipt(tmp_path, stage):
    from test_execution_control import (
        test_inflight_publication_cancellation_reconciles_effect_without_new_writes as probe,
    )

    assert make_url(POSTGRES_URL).database == "repopilot_ch06a_test"
    db = Database(POSTGRES_URL)
    await db.setup()
    try:
        await probe(db, tmp_path, stage)
        print(f"CH-06A postgres_cancel stage={stage} effect_reconciled=true")
    finally:
        await db.close()


async def test_postgres_cancel_race_has_one_request_and_stale_completion_is_rejected():
    from repopilot.db import TaskStateConflict

    assert make_url(POSTGRES_URL).database == "repopilot_ch06a_test"
    db = Database(POSTGRES_URL)
    await db.setup()
    try:
        queued = await db.create_task(task_data())
        task = await db.transition_task(
            queued.id,
            expected_status=queued.status,
            expected_version=queued.state_version,
            status=TaskStatus.RUNNING,
        )
        requests = await asyncio.gather(db.request_stop(task.id), db.request_stop(task.id))
        assert all(
            r.status == TaskStatus.CANCELLING and r.state_version == task.state_version + 1
            for r in requests
        )
        assert len(await db.list_events(task.id)) == 1
        with pytest.raises(TaskStateConflict):
            await db.transition_task(
                task.id,
                expected_status=task.status,
                expected_version=task.state_version,
                status=TaskStatus.COMPLETED,
            )
        stopped = await db.confirm_stop(task.id, expected_version=requests[0].state_version)
        assert stopped.status == TaskStatus.CANCELLED and stopped.stopped_at
        print(
            "CH-06A postgres_cancel concurrent_requests=2 durable_requests=1 "
            "stale_completion=rejected"
        )
    finally:
        await db.close()
