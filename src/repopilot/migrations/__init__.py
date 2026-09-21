from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import create_async_engine

BASELINE_REVISION = "20260921_0001"
HEAD_REVISION = "20260921_0003"

_LEGACY_TASK_COLUMNS = {
    "id",
    "status",
    "repository_url",
    "issue_title",
    "issue_body",
    "base_branch",
    "test_command",
    "max_iterations",
    "graph_thread_id",
    "result",
    "error",
    "created_at",
    "updated_at",
}
_LEGACY_EVENT_COLUMNS = {
    "id",
    "task_id",
    "kind",
    "node",
    "message",
    "payload",
    "created_at",
}


class SchemaKind(StrEnum):
    EMPTY = "empty"
    LEGACY = "legacy"
    VERSIONED = "versioned"


class UnrecognizedDatabaseSchema(RuntimeError):
    """Raised instead of guessing how to migrate an unknown or partial schema."""


@dataclass(frozen=True, slots=True)
class SchemaInspection:
    kind: SchemaKind
    tables: tuple[str, ...]


def alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent))
    # Alembic Config uses ConfigParser interpolation for main options.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _inspect_sync(connection: Connection) -> SchemaInspection:
    schema = inspect(connection)
    tables = set(schema.get_table_names())
    if "alembic_version" in tables:
        return SchemaInspection(SchemaKind.VERSIONED, tuple(sorted(tables)))

    app_tables = tables & {"tasks", "task_events"}
    if not app_tables:
        return SchemaInspection(SchemaKind.EMPTY, tuple(sorted(tables)))
    if app_tables != {"tasks", "task_events"}:
        raise UnrecognizedDatabaseSchema(
            "Unversioned database has only part of the RepoPilot schema; "
            "restore from backup or provide an explicit forward migration"
        )

    task_columns = {column["name"] for column in schema.get_columns("tasks")}
    event_columns = {column["name"] for column in schema.get_columns("task_events")}
    if task_columns != _LEGACY_TASK_COLUMNS or event_columns != _LEGACY_EVENT_COLUMNS:
        raise UnrecognizedDatabaseSchema(
            "Unversioned RepoPilot tables do not match the exact pre-migration schema; "
            "automatic stamping is refused"
        )
    return SchemaInspection(SchemaKind.LEGACY, tuple(sorted(tables)))


async def inspect_database_schema(database_url: str) -> SchemaInspection:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_inspect_sync)
    finally:
        await engine.dispose()


def _stamp(database_url: str, revision: str) -> None:
    command.stamp(alembic_config(database_url), revision)


def _upgrade(database_url: str, revision: str) -> None:
    command.upgrade(alembic_config(database_url), revision)


async def ensure_database_schema(
    database_url: str,
    *,
    revision: str = "head",
) -> SchemaInspection:
    """Install or upgrade RepoPilot-owned tables without touching LangGraph tables.

    An exact unversioned legacy schema is stamped at the baseline revision and then upgraded.
    Partial or modified unversioned schemas are rejected so startup cannot guess at recovery.
    """

    before = await inspect_database_schema(database_url)
    if before.kind is SchemaKind.LEGACY:
        await asyncio.to_thread(_stamp, database_url, BASELINE_REVISION)
    await asyncio.to_thread(_upgrade, database_url, revision)
    return before


def _current_revision_sync(connection: Connection) -> tuple[str, ...]:
    context = MigrationContext.configure(connection)
    return tuple(context.get_current_heads())


async def current_database_revisions(database_url: str) -> tuple[str, ...]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_current_revision_sync)
    finally:
        await engine.dispose()


__all__ = [
    "BASELINE_REVISION",
    "HEAD_REVISION",
    "SchemaInspection",
    "SchemaKind",
    "UnrecognizedDatabaseSchema",
    "current_database_revisions",
    "ensure_database_schema",
    "inspect_database_schema",
]
