from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from repopilot.models import EventView, TaskCreate, TaskStatus, TaskView


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


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
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
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
            result=self.result,
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_view(self) -> EventView:
        return EventView(
            id=self.id,
            task_id=self.task_id,
            kind=self.kind,
            node=self.node,
            message=self.message,
            payload=self.payload or {},
            created_at=self.created_at,
        )


class Database:
    def __init__(self, url: str):
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    async def setup(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

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

    async def update_task(self, task_id: str, **values: Any) -> TaskView:
        async with self.sessions() as session:
            record = await session.get(TaskRecord, task_id)
            if record is None:
                raise KeyError(task_id)
            for key, value in values.items():
                if key == "status" and isinstance(value, TaskStatus):
                    value = value.value
                setattr(record, key, value)
            record.updated_at = utcnow()
            await session.commit()
            await session.refresh(record)
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
