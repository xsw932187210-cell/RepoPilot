from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from repopilot.db import Database
from repopilot.migrations import HEAD_REVISION, alembic_config, current_database_revisions
from repopilot.model_calls import (
    CallBudgetExceeded,
    CallIdentity,
    CallPolicy,
    ControlledModelCaller,
)
from repopilot.models import TaskCreate

POSTGRES_URL = os.getenv("REPOPILOT_CH10_POSTGRES_TEST_URL", "")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set REPOPILOT_CH10_POSTGRES_TEST_URL for the isolated CH-10 PostgreSQL probe",
)


async def reset_owned_tables() -> None:
    engine = create_async_engine(POSTGRES_URL)
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text("DROP TABLE IF EXISTS model_call_attempts CASCADE"))
            await connection.execute(sa.text("DROP TABLE IF EXISTS model_call_budgets CASCADE"))
            await connection.execute(sa.text("DROP TABLE IF EXISTS task_events CASCADE"))
            await connection.execute(sa.text("DROP TABLE IF EXISTS tasks CASCADE"))
            await connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version CASCADE"))
    finally:
        await engine.dispose()


async def table_names() -> set[str]:
    engine = create_async_engine(POSTGRES_URL)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    finally:
        await engine.dispose()


def task_data(title: str) -> TaskCreate:
    return TaskCreate(
        repository_url="demo://buggy-calculator",
        issue_title=title,
        issue_body="Preserve the task while upgrading the CH-09 database to CH-10.",
    )


@pytest.mark.asyncio
async def test_postgres_ch10_empty_install_ch09_upgrade_idempotency_and_failure_recovery() -> None:
    assert make_url(POSTGRES_URL).database == "repopilot_ch10_test", (
        "The CH-10 PostgreSQL probe refuses non-dedicated databases"
    )

    await reset_owned_tables()
    empty = Database(POSTGRES_URL)
    await empty.setup()
    await empty.setup()
    empty_task = await empty.create_task(task_data("CH-10 empty install"))
    policy = CallPolicy(max_calls=2, request_timeout_seconds=5)
    ledger = empty.model_call_ledger(empty_task.id)
    await ledger.ensure(policy)
    caller = ControlledModelCaller(ledger, policy)

    async def transport() -> dict[str, object]:
        return {
            "usage_metadata": {
                "input_tokens": 2,
                "output_tokens": 1,
                "total_tokens": 3,
            }
        }

    await caller.call(
        transport,
        call_identity=CallIdentity(
            provider="fixture-provider",
            model="fixture-model",
            adapter_version="fixture-adapter-v1",
            request_schema_version="fixture-schema-v1",
        ),
        call_role="planner",
    )
    empty_metrics = await empty.get_model_call_metrics(empty_task.id)
    await empty.close()
    assert await current_database_revisions(POSTGRES_URL) == (HEAD_REVISION,)
    assert empty_metrics is not None
    assert empty_metrics["successful_model_calls"] == 1

    # Install the exact CH-09 head, write an old task/event, then upgrade twice to CH-10.
    await reset_owned_tables()
    await asyncio.to_thread(command.upgrade, alembic_config(POSTGRES_URL), "20260921_0002")
    ch09 = Database(POSTGRES_URL)
    retained = await ch09.create_task(task_data("Retained CH-09 task"))
    await ch09.add_event(
        retained.id,
        kind="ch09_event",
        node="fixture",
        message="retain across CH-10 migration",
    )
    await ch09.close()
    await asyncio.to_thread(command.upgrade, alembic_config(POSTGRES_URL), "head")
    await asyncio.to_thread(command.upgrade, alembic_config(POSTGRES_URL), "head")
    upgraded = Database(POSTGRES_URL)
    retained_after = await upgraded.get_task(retained.id)
    retained_events = await upgraded.list_events(retained.id)
    await upgraded.close()
    assert retained_after is not None
    assert len(retained_events) == 1
    assert {"model_call_budgets", "model_call_attempts"} <= await table_names()

    # PostgreSQL transactional DDL must leave the last good revision recoverable.
    await reset_owned_tables()
    await asyncio.to_thread(command.upgrade, alembic_config(POSTGRES_URL), "20260921_0002")
    engine = create_async_engine(POSTGRES_URL)
    try:
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text("CREATE TABLE ch10_failed_migration_probe (id INTEGER)")
                )
                await connection.execute(sa.text("SELECT 1 / 0"))
        async with engine.connect() as connection:
            names_after_failure = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
            server_version = (await connection.execute(sa.text("SHOW server_version"))).scalar_one()
    finally:
        await engine.dispose()
    assert "ch10_failed_migration_probe" not in names_after_failure
    assert await current_database_revisions(POSTGRES_URL) == ("20260921_0002",)

    await asyncio.to_thread(command.upgrade, alembic_config(POSTGRES_URL), "head")
    assert await current_database_revisions(POSTGRES_URL) == (HEAD_REVISION,)
    print(
        f"postgres_server_version={server_version} migration_revision={HEAD_REVISION} "
        f"task_retained={retained.id} recovery=passed"
    )


@pytest.mark.asyncio
async def test_postgres_atomic_reservations_never_exceed_task_budget() -> None:
    await reset_owned_tables()
    database = Database(POSTGRES_URL)
    await database.setup()
    task = await database.create_task(task_data("Concurrent reservation cap"))
    policy = CallPolicy(max_calls=1)
    first = database.model_call_ledger(task.id)
    second = database.model_call_ledger(task.id)
    await asyncio.gather(first.ensure(policy), second.ensure(policy))

    async def reserve(ledger, logical_call_id: str) -> str:
        try:
            reservation = await ledger.reserve(
                logical_call_id=logical_call_id,
                role="coder",
                identity=CallIdentity(
                    provider="fixture-provider",
                    model="fixture-model",
                    adapter_version="fixture-adapter-v1",
                    request_schema_version="fixture-schema-v1",
                ),
                retry_index=0,
            )
            return reservation.attempt_id
        except CallBudgetExceeded as error:
            return type(error).__name__

    outcomes = await asyncio.gather(
        reserve(first, str(uuid.uuid4())),
        reserve(second, str(uuid.uuid4())),
    )
    metrics = await database.get_model_call_metrics(task.id)
    attempts = await database.list_model_call_attempts(task.id)
    await database.close()
    assert sum(outcome == "CallBudgetExceeded" for outcome in outcomes) == 1
    assert metrics is not None
    assert metrics["reserved_calls"] == 1
    assert len(attempts) == 1
