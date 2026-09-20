from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from repopilot.migrations import ensure_database_schema
from repopilot.models import (
    ALLOWED_TASK_TRANSITIONS,
    EVENT_PAYLOAD_SCHEMA_VERSION,
    TASK_RESULT_SCHEMA_VERSION,
    EventView,
    TaskCreate,
    TaskStatus,
    TaskView,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


_UNCHANGED = object()


class Base(DeclarativeBase):
    pass


class InvalidTaskTransition(ValueError):
    def __init__(self, current: TaskStatus, requested: TaskStatus) -> None:
        self.current = current
        self.requested = requested
        super().__init__(f"Task transition {current.value} -> {requested.value} is not allowed")


class TaskStateConflict(RuntimeError):
    def __init__(
        self,
        task_id: str,
        *,
        expected_status: TaskStatus,
        expected_version: int,
        current_status: TaskStatus | None,
        current_version: int | None,
    ) -> None:
        self.task_id = task_id
        self.expected_status = expected_status
        self.expected_version = expected_version
        self.current_status = current_status
        self.current_version = current_version
        current = (
            "missing"
            if current_status is None
            else f"{current_status.value}@{current_version}"
        )
        super().__init__(
            f"Task {task_id} state conflict: expected "
            f"{expected_status.value}@{expected_version}, current {current}"
        )


class TaskRecord(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    repository_url: Mapped[str] = mapped_column(String(500))
    issue_title: Mapped[str] = mapped_column(String(240))
    issue_body: Mapped[str] = mapped_column(Text)
    base_branch: Mapped[str] = mapped_column(String(120))
    test_command: Mapped[str] = mapped_column(String(300))
    max_iterations: Mapped[int] = mapped_column(Integer, default=2)
    graph_thread_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    state_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1")
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result_schema_version: Mapped[int] = mapped_column(
        Integer,
        default=TASK_RESULT_SCHEMA_VERSION,
        server_default=text("0"),
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    def to_view(self) -> TaskView:
        return TaskView(
            id=self.id,
            status=TaskStatus(self.status),
            repository_url=self.repository_url,
            issue_title=self.issue_title,
            issue_body=self.issue_body,
            base_branch=self.base_branch,
            test_command=self.test_command,
            max_iterations=self.max_iterations,
            graph_thread_id=self.graph_thread_id,
            state_version=self.state_version,
            result=self.result,
            result_schema_version=self.result_schema_version,
            error=self.error,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class EventRecord(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(60))
    node: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    payload_schema_version: Mapped[int] = mapped_column(
        Integer,
        default=EVENT_PAYLOAD_SCHEMA_VERSION,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_view(self) -> EventView:
        return EventView(
            id=self.id,
            task_id=self.task_id,
            kind=self.kind,
            node=self.node,
            message=self.message,
            payload=self.payload or {},
            payload_schema_version=self.payload_schema_version,
            created_at=self.created_at,
        )


class Database:
    def __init__(self, url: str):
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    async def setup(self) -> None:
        await ensure_database_schema(self.url)

    async def close(self) -> None:
        await self.engine.dispose()

    async def create_task(self, data: TaskCreate) -> TaskView:
        task_id = str(uuid.uuid4())
        record = TaskRecord(
            id=task_id,
            status=TaskStatus.QUEUED.value,
            repository_url=data.repository_url,
            issue_title=data.issue_title,
            issue_body=data.issue_body,
            base_branch=data.base_branch,
            test_command=data.test_command,
            max_iterations=data.max_iterations,
            graph_thread_id=f"task-{task_id}",
            state_version=1,
            result_schema_version=TASK_RESULT_SCHEMA_VERSION,
        )
        async with self.sessions() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
        return record.to_view()

    async def get_task(self, task_id: str) -> TaskView | None:
        async with self.sessions() as session:
            record = await session.get(TaskRecord, task_id)
            return record.to_view() if record else None

    async def get_task_record(self, task_id: str) -> TaskRecord | None:
        async with self.sessions() as session:
            return await session.get(TaskRecord, task_id)

    async def transition_task(
        self,
        task_id: str,
        *,
        expected_status: TaskStatus,
        expected_version: int,
        status: TaskStatus,
        result: dict[str, Any] | None | object = _UNCHANGED,
        error: str | None | object = _UNCHANGED,
    ) -> TaskView:
        if status not in ALLOWED_TASK_TRANSITIONS[expected_status]:
            raise InvalidTaskTransition(expected_status, status)

        values: dict[str, Any] = {
            "status": status.value,
            "state_version": TaskRecord.state_version + 1,
            "updated_at": utcnow(),
        }
        if result is not _UNCHANGED:
            values["result"] = result
            values["result_schema_version"] = TASK_RESULT_SCHEMA_VERSION
        if error is not _UNCHANGED:
            values["error"] = error

        async with self.sessions() as session:
            outcome = await session.execute(
                update(TaskRecord)
                .where(
                    TaskRecord.id == task_id,
                    TaskRecord.status == expected_status.value,
                    TaskRecord.state_version == expected_version,
                )
                .values(**values)
            )
            if outcome.rowcount != 1:
                await session.rollback()
                current = await session.get(TaskRecord, task_id)
                if current is None:
                    raise KeyError(task_id)
                raise TaskStateConflict(
                    task_id,
                    expected_status=expected_status,
                    expected_version=expected_version,
                    current_status=TaskStatus(current.status),
                    current_version=current.state_version,
                )
            await session.commit()
            record = await session.get(TaskRecord, task_id)
            if record is None:  # The primary key cannot disappear inside this transaction.
                raise KeyError(task_id)
            return record.to_view()

    async def add_event(
        self,
        task_id: str,
        *,
        kind: str,
        node: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> EventView:
        record = EventRecord(
            task_id=task_id,
            kind=kind,
            node=node,
            message=message,
            payload=payload or {},
            payload_schema_version=EVENT_PAYLOAD_SCHEMA_VERSION,
        )
        async with self.sessions() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
        return record.to_view()

    async def list_events(self, task_id: str, after_id: int = 0) -> list[EventView]:
        async with self.sessions() as session:
            result = await session.execute(
                select(EventRecord)
                .where(EventRecord.task_id == task_id, EventRecord.id > after_id)
                .order_by(EventRecord.id.asc())
            )
            return [record.to_view() for record in result.scalars()]
