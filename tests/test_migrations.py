from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

import repopilot.migrations as migrations
from repopilot.db import Database, InvalidTaskTransition, TaskStateConflict
from repopilot.migrations import (
    BASELINE_REVISION,
    HEAD_REVISION,
    SchemaKind,
    UnrecognizedDatabaseSchema,
    current_database_revisions,
    ensure_database_schema,
)
from repopilot.models import TaskCreate, TaskStatus


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


async def install_legacy_database(database_url: str, task_id: str) -> None:
    metadata, tasks, events = legacy_metadata()
    engine = create_async_engine(database_url)
    now = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.execute(
                tasks.insert().values(
                    id=task_id,
                    status=TaskStatus.QUEUED.value,
                    repository_url="demo://buggy-calculator",
                    issue_title="Legacy migration fixture",
                    issue_body="Preserve this task while upgrading the real old structure.",
                    base_branch="main",
                    test_command="python -m pytest -q",
                    max_iterations=2,
                    graph_thread_id=f"task-{task_id}",
                    result={"legacy": True},
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
                    message="preserve me",
                    payload={"legacy": True},
                    created_at=now,
                )
            )
    finally:
        await engine.dispose()


async def sqlite_columns(database_url: str, table: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync: {column["name"] for column in inspect(sync).get_columns(table)}
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_sqlite_install_and_repeat_are_idempotent(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"
    database = Database(database_url)
    await database.setup()
    await database.setup()

    assert await current_database_revisions(database_url) == (HEAD_REVISION,)
    assert {"state_version", "result_schema_version"} <= await sqlite_columns(
        database_url, "tasks"
    )
    assert "payload_schema_version" in await sqlite_columns(database_url, "task_events")

    task = await database.create_task(
        TaskCreate(
            repository_url="demo://buggy-calculator",
            issue_title="New versioned task",
            issue_body="Verify defaults for a newly installed database.",
        )
    )
    event = await database.add_event(
        task.id,
        kind="created",
        node="test",
        message="created",
    )
    await database.close()

    assert task.state_version == 1
    assert task.result_schema_version == 1
    assert event.payload_schema_version == 1


@pytest.mark.asyncio
async def test_legacy_sqlite_upgrade_preserves_records_and_marks_json_version(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
    task_id = str(uuid.uuid4())
    await install_legacy_database(database_url, task_id)

    before = await ensure_database_schema(database_url)
    assert before.kind is SchemaKind.LEGACY
    assert await current_database_revisions(database_url) == (HEAD_REVISION,)

    database = Database(database_url)
    task = await database.get_task(task_id)
    events = await database.list_events(task_id)
    assert task is not None
    assert task.result == {"legacy": True}
    assert task.state_version == 1
    assert task.result_schema_version == 0
    assert len(events) == 1
    assert events[0].payload == {"legacy": True}
    assert events[0].payload_schema_version == 0

    running = await database.transition_task(
        task_id,
        expected_status=TaskStatus.QUEUED,
        expected_version=task.state_version,
        status=TaskStatus.RUNNING,
    )
    completed = await database.transition_task(
        task_id,
        expected_status=TaskStatus.RUNNING,
        expected_version=running.state_version,
        status=TaskStatus.COMPLETED,
        result={"status": "completed"},
    )
    new_event = await database.add_event(
        task_id,
        kind="completed",
        node="test",
        message="new schema payload",
        payload={"status": "completed"},
    )
    await database.close()

    assert completed.state_version == 3
    assert completed.result_schema_version == 1
    assert new_event.payload_schema_version == 1


@pytest.mark.asyncio
async def test_task_updates_reject_stale_and_illegal_transitions(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'cas.db'}"
    database = Database(database_url)
    await database.setup()
    queued = await database.create_task(
        TaskCreate(
            repository_url="demo://buggy-calculator",
            issue_title="State conflict",
            issue_body="Only the holder of the current task version may update it.",
        )
    )
    running = await database.transition_task(
        queued.id,
        expected_status=TaskStatus.QUEUED,
        expected_version=queued.state_version,
        status=TaskStatus.RUNNING,
    )

    with pytest.raises(TaskStateConflict) as conflict:
        await database.transition_task(
            queued.id,
            expected_status=TaskStatus.QUEUED,
            expected_version=queued.state_version,
            status=TaskStatus.CANCELLED,
        )
    assert conflict.value.current_status is TaskStatus.RUNNING
    assert conflict.value.current_version == running.state_version

    with pytest.raises(InvalidTaskTransition):
        await database.transition_task(
            queued.id,
            expected_status=TaskStatus.RUNNING,
            expected_version=running.state_version,
            status=TaskStatus.QUEUED,
        )

    unchanged = await database.get_task(queued.id)
    await database.close()
    assert unchanged is not None
    assert unchanged.status is TaskStatus.RUNNING
    assert unchanged.state_version == running.state_version


@pytest.mark.asyncio
async def test_unknown_unversioned_schema_is_not_guessed(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'partial.db'}"
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text("CREATE TABLE tasks (id VARCHAR(36) PRIMARY KEY)"))
    finally:
        await engine.dispose()

    with pytest.raises(UnrecognizedDatabaseSchema, match="only part"):
        await ensure_database_schema(database_url)


@pytest.mark.asyncio
async def test_interrupted_legacy_upgrade_resumes_from_stamped_baseline(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'interrupted.db'}"
    task_id = str(uuid.uuid4())
    await install_legacy_database(database_url, task_id)
    real_upgrade = migrations._upgrade

    def fail_after_stamp(database_url: str, revision: str) -> None:
        del database_url, revision
        raise RuntimeError("synthetic migration interruption")

    monkeypatch.setattr(migrations, "_upgrade", fail_after_stamp)
    with pytest.raises(RuntimeError, match="synthetic migration interruption"):
        await ensure_database_schema(database_url)
    assert await current_database_revisions(database_url) == (BASELINE_REVISION,)

    monkeypatch.setattr(migrations, "_upgrade", real_upgrade)
    before_retry = await ensure_database_schema(database_url)
    assert before_retry.kind is SchemaKind.VERSIONED
    assert await current_database_revisions(database_url) == (HEAD_REVISION,)

    database = Database(database_url)
    restored = await database.get_task(task_id)
    await database.close()
    assert restored is not None
    assert restored.result == {"legacy": True}
