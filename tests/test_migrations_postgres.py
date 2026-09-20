from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from repopilot.db import Database
from repopilot.migrations import (
    BASELINE_REVISION,
    HEAD_REVISION,
    alembic_config,
    current_database_revisions,
    ensure_database_schema,
)
from repopilot.models import TaskStatus

POSTGRES_URL = os.getenv("REPOPILOT_POSTGRES_TEST_URL", "")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set REPOPILOT_POSTGRES_TEST_URL for the isolated PostgreSQL probe",
)


def legacy_metadata() -> tuple[sa.MetaData, sa.Table, sa.Table]:
    metadata = sa.MetaData()
    tasks = sa.Table(
        "tasks",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(40), nullable=False, index=True),
        sa.Column("repository_url", sa.String(500), nullable=False),
        sa.Column("issue_title", sa.String(240), nullable=False),
        sa.Column("issue_body", sa.Text(), nullable=False),
        sa.Column("base_branch", sa.String(120), nullable=False),
        sa.Column("test_command", sa.String(300), nullable=False),
        sa.Column("max_iterations", sa.Integer(), nullable=False),
        sa.Column("graph_thread_id", sa.String(80), nullable=False, unique=True, index=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    events = sa.Table(
        "task_events",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("kind", sa.String(60), nullable=False),
        sa.Column("node", sa.String(80), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    return metadata, tasks, events


async def reset_owned_tables() -> None:
    engine = create_async_engine(POSTGRES_URL)
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text("DROP TABLE IF EXISTS task_events CASCADE"))
            await connection.execute(sa.text("DROP TABLE IF EXISTS tasks CASCADE"))
            await connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version CASCADE"))
    finally:
        await engine.dispose()


async def install_legacy_fixture(task_id: str) -> None:
    metadata, tasks, events = legacy_metadata()
    engine = create_async_engine(POSTGRES_URL)
    now = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(
                tasks.insert().values(
                    id=task_id,
                    status=TaskStatus.QUEUED.value,
                    repository_url="demo://buggy-calculator",
                    issue_title="PostgreSQL legacy fixture",
                    issue_body="Preserve rows across the isolated integration upgrade.",
                    base_branch="main",
                    test_command="python -m pytest -q",
                    max_iterations=2,
                    graph_thread_id=f"task-{task_id}",
                    result={"database": "postgresql"},
                    error=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            await connection.execute(
                events.insert().values(
                    task_id=task_id,
                    kind="legacy_event",
                    node="fixture",
                    message="preserve PostgreSQL event",
                    payload={"database": "postgresql"},
                    created_at=now,
                )
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_upgrade_idempotency_and_failed_ddl_recovery() -> None:
    assert make_url(POSTGRES_URL).database == "repopilot_ch09_test", (
        "The PostgreSQL migration probe refuses non-dedicated databases"
    )

    await reset_owned_tables()
    first_task_id = str(uuid.uuid4())
    await install_legacy_fixture(first_task_id)
    await ensure_database_schema(POSTGRES_URL)
    await ensure_database_schema(POSTGRES_URL)
    assert await current_database_revisions(POSTGRES_URL) == (HEAD_REVISION,)

    database = Database(POSTGRES_URL)
    first_task = await database.get_task(first_task_id)
    first_events = await database.list_events(first_task_id)
    await database.close()
    assert first_task is not None
    assert first_task.result == {"database": "postgresql"}
    assert first_task.result_schema_version == 0
    assert first_events[0].payload_schema_version == 0

    # Recreate the exact legacy schema, stamp the recognized baseline, then prove that a
    # real PostgreSQL DDL failure rolls back and a forward retry reaches head without data loss.
    await reset_owned_tables()
    recovery_task_id = str(uuid.uuid4())
    await install_legacy_fixture(recovery_task_id)
    await asyncio.to_thread(command.stamp, alembic_config(POSTGRES_URL), BASELINE_REVISION)

    engine = create_async_engine(POSTGRES_URL)
    try:
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text("ALTER TABLE tasks ADD COLUMN failed_migration_probe INTEGER")
                )
                await connection.execute(sa.text("SELECT 1 / 0"))

        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: {
                    column["name"] for column in inspect(sync).get_columns("tasks")
                }
            )
            server_version = (await connection.execute(sa.text("SHOW server_version"))).scalar_one()
    finally:
        await engine.dispose()

    assert "failed_migration_probe" not in columns
    assert server_version.startswith("16.")
    assert await current_database_revisions(POSTGRES_URL) == (BASELINE_REVISION,)

    await ensure_database_schema(POSTGRES_URL)
    assert await current_database_revisions(POSTGRES_URL) == (HEAD_REVISION,)
    recovered = Database(POSTGRES_URL)
    recovered_task = await recovered.get_task(recovery_task_id)
    recovered_events = await recovered.list_events(recovery_task_id)
    await recovered.close()
    assert recovered_task is not None
    assert recovered_task.result == {"database": "postgresql"}
    assert len(recovered_events) == 1
    print(
        f"postgres_server_version={server_version} "
        f"migration_revision={HEAD_REVISION} recovery=passed"
    )
