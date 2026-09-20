from __future__ import annotations

import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from repopilot.migrations import ensure_database_schema
from repopilot.model_calls import (
    BackoffBudgetExceeded,
    CallBudgetExceeded,
    CallIdentity,
    CallLedgerStateConflict,
    CallPolicy,
    CallPolicyMismatch,
    CallReservation,
    CallStatus,
    CallUsage,
    TokenBudgetExceeded,
)
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


class ModelCallBudgetRecord(Base):
    __tablename__ = "model_call_budgets"

    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    policy_version: Mapped[str] = mapped_column(String(80))
    policy_fingerprint: Mapped[str] = mapped_column(String(64))
    max_calls: Mapped[int] = mapped_column(Integer)
    request_timeout_seconds: Mapped[float] = mapped_column(Float)
    max_rate_limit_retries: Mapped[int] = mapped_column(Integer)
    max_transient_retries: Mapped[int] = mapped_column(Integer)
    base_backoff_seconds: Mapped[float] = mapped_column(Float)
    max_retry_wait_seconds: Mapped[float] = mapped_column(Float)
    max_total_backoff_seconds: Mapped[float] = mapped_column(Float)
    max_total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int] = mapped_column(Integer)

    reserved_calls: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    started_calls: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    succeeded_calls: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    failed_calls: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    unknown_calls: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    rate_limit_retries: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    transient_retries: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    backoff_seconds: Mapped[float] = mapped_column(Float, default=0, server_default=text("0"))
    observed_input_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    observed_output_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    observed_total_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    usage_complete: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ModelCallAttemptRecord(Base):
    __tablename__ = "model_call_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "sequence_no", name="uq_model_call_attempt_task_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("model_call_budgets.task_id", ondelete="CASCADE"),
        index=True,
    )
    sequence_no: Mapped[int] = mapped_column(Integer)
    logical_call_id: Mapped[str] = mapped_column(String(36), index=True)
    role: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(200))
    adapter_version: Mapped[str] = mapped_column(String(100))
    request_schema_version: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), index=True)
    retry_index: Mapped[int] = mapped_column(Integer)
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_from_provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    fallback_from_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    wait_seconds: Mapped[float] = mapped_column(Float, default=0, server_default=text("0"))
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    def model_call_ledger(self, task_id: str) -> DatabaseCallLedger:
        return DatabaseCallLedger(self, task_id)

    async def get_model_call_metrics(
        self, task_id: str
    ) -> dict[str, int | float | bool | str | None] | None:
        async with self.sessions() as session:
            if await session.get(ModelCallBudgetRecord, task_id) is None:
                return None
        return await self.model_call_ledger(task_id).snapshot()

    async def list_model_call_attempts(self, task_id: str) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            result = await session.execute(
                select(ModelCallAttemptRecord)
                .where(ModelCallAttemptRecord.task_id == task_id)
                .order_by(ModelCallAttemptRecord.sequence_no)
            )
            return [
                {
                    "id": attempt.id,
                    "sequence_no": attempt.sequence_no,
                    "logical_call_id": attempt.logical_call_id,
                    "role": attempt.role,
                    "provider": attempt.provider,
                    "model": attempt.model,
                    "adapter_version": attempt.adapter_version,
                    "request_schema_version": attempt.request_schema_version,
                    "status": attempt.status,
                    "retry_index": attempt.retry_index,
                    "is_fallback": attempt.is_fallback,
                    "fallback_from_provider": attempt.fallback_from_provider,
                    "fallback_from_model": attempt.fallback_from_model,
                    "wait_seconds": attempt.wait_seconds,
                    "error_code": attempt.error_code,
                    "input_tokens": attempt.input_tokens,
                    "output_tokens": attempt.output_tokens,
                    "total_tokens": attempt.total_tokens,
                }
                for attempt in result.scalars()
            ]


class DatabaseCallLedger:
    """CH-10's persistent call ledger for one task.

    A budget reservation and a provider request cannot share a transaction. The reservation
    commits first; STARTED commits immediately before transport execution. Recovery leaves
    RESERVED entries consumed and converts STARTED entries to UNKNOWN conservatively.
    """

    def __init__(self, database: Database, task_id: str) -> None:
        self.database = database
        self.task_id = task_id
        self.policy: CallPolicy | None = None

    async def ensure(self, policy: CallPolicy) -> None:
        async with self.database.sessions() as session:
            record = await session.get(ModelCallBudgetRecord, self.task_id)
            if record is None:
                values = policy.persisted_values()
                session.add(
                    ModelCallBudgetRecord(
                        task_id=self.task_id,
                        policy_fingerprint=policy.fingerprint,
                        **values,
                    )
                )
                try:
                    await session.commit()
                except IntegrityError:
                    # Another worker may have initialized this task's budget after
                    # our read. The unique task_id makes the winner authoritative;
                    # verify that it installed the same immutable policy.
                    await session.rollback()
                    record = await session.get(ModelCallBudgetRecord, self.task_id)
                    if record is None:
                        raise
            if record is not None and record.policy_fingerprint != policy.fingerprint:
                raise CallPolicyMismatch()
        self.policy = policy

    def _require_policy(self) -> CallPolicy:
        if self.policy is None:
            raise RuntimeError("model call ledger must be initialized before use")
        return self.policy

    async def reconcile_incomplete(self) -> int:
        self._require_policy()
        now = utcnow()
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(ModelCallAttemptRecord)
                .where(
                    ModelCallAttemptRecord.task_id == self.task_id,
                    ModelCallAttemptRecord.status == CallStatus.STARTED.value,
                )
                .values(
                    status=CallStatus.UNKNOWN.value,
                    error_code="model_response_unknown_after_recovery",
                    finished_at=now,
                )
                .returning(ModelCallAttemptRecord.id)
            )
            reconciled = len(result.scalars().all())
            if reconciled:
                await session.execute(
                    update(ModelCallBudgetRecord)
                    .where(ModelCallBudgetRecord.task_id == self.task_id)
                    .values(
                        unknown_calls=ModelCallBudgetRecord.unknown_calls + reconciled,
                        usage_complete=False,
                        updated_at=now,
                    )
                )
        return reconciled

    async def reserve(
        self,
        *,
        logical_call_id: str,
        role: str,
        identity: CallIdentity,
        retry_index: int,
    ) -> CallReservation:
        self._require_policy()
        attempt_id = str(uuid.uuid4())
        now = utcnow()
        token_available = or_(
            ModelCallBudgetRecord.max_total_tokens.is_(None),
            ModelCallBudgetRecord.usage_complete.is_(False),
            ModelCallBudgetRecord.observed_total_tokens
            < ModelCallBudgetRecord.max_total_tokens,
        )
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(ModelCallBudgetRecord)
                .where(
                    ModelCallBudgetRecord.task_id == self.task_id,
                    ModelCallBudgetRecord.reserved_calls < ModelCallBudgetRecord.max_calls,
                    token_available,
                )
                .values(
                    reserved_calls=ModelCallBudgetRecord.reserved_calls + 1,
                    updated_at=now,
                )
                .returning(ModelCallBudgetRecord.reserved_calls)
            )
            sequence_no = result.scalar_one_or_none()
            if sequence_no is None:
                current = await session.get(ModelCallBudgetRecord, self.task_id)
                if current is None:
                    raise KeyError(self.task_id)
                if (
                    current.max_total_tokens is not None
                    and current.usage_complete
                    and current.observed_total_tokens >= current.max_total_tokens
                ):
                    raise TokenBudgetExceeded(
                        current.observed_total_tokens, current.max_total_tokens
                    )
                raise CallBudgetExceeded(current.reserved_calls, current.max_calls)
            session.add(
                ModelCallAttemptRecord(
                    id=attempt_id,
                    task_id=self.task_id,
                    sequence_no=sequence_no,
                    logical_call_id=logical_call_id,
                    role=role,
                    provider=identity.provider,
                    model=identity.model,
                    adapter_version=identity.adapter_version,
                    request_schema_version=identity.request_schema_version,
                    status=CallStatus.RESERVED.value,
                    retry_index=retry_index,
                    is_fallback=identity.is_fallback,
                    fallback_from_provider=identity.fallback_from_provider,
                    fallback_from_model=identity.fallback_from_model,
                    reserved_at=now,
                )
            )
        return CallReservation(attempt_id, sequence_no, logical_call_id)

    async def mark_started(self, attempt_id: str) -> None:
        now = utcnow()
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(ModelCallAttemptRecord)
                .where(
                    ModelCallAttemptRecord.id == attempt_id,
                    ModelCallAttemptRecord.task_id == self.task_id,
                    ModelCallAttemptRecord.status == CallStatus.RESERVED.value,
                )
                .values(status=CallStatus.STARTED.value, started_at=now)
                .returning(ModelCallAttemptRecord.id)
            )
            if result.scalar_one_or_none() is None:
                raise CallLedgerStateConflict(attempt_id, CallStatus.RESERVED)
            await session.execute(
                update(ModelCallBudgetRecord)
                .where(ModelCallBudgetRecord.task_id == self.task_id)
                .values(
                    started_calls=ModelCallBudgetRecord.started_calls + 1,
                    updated_at=now,
                )
            )

    async def mark_succeeded(self, attempt_id: str, usage: CallUsage | None) -> None:
        now = utcnow()
        attempt_values: dict[str, Any] = {
            "status": CallStatus.SUCCEEDED.value,
            "finished_at": now,
        }
        budget_values: dict[str, Any] = {
            "succeeded_calls": ModelCallBudgetRecord.succeeded_calls + 1,
            "updated_at": now,
        }
        if usage is None:
            budget_values["usage_complete"] = False
        else:
            attempt_values.update(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
            )
            budget_values.update(
                observed_input_tokens=(
                    ModelCallBudgetRecord.observed_input_tokens + usage.input_tokens
                ),
                observed_output_tokens=(
                    ModelCallBudgetRecord.observed_output_tokens + usage.output_tokens
                ),
                observed_total_tokens=(
                    ModelCallBudgetRecord.observed_total_tokens + usage.total_tokens
                ),
            )
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(ModelCallAttemptRecord)
                .where(
                    ModelCallAttemptRecord.id == attempt_id,
                    ModelCallAttemptRecord.task_id == self.task_id,
                    ModelCallAttemptRecord.status == CallStatus.STARTED.value,
                )
                .values(**attempt_values)
                .returning(ModelCallAttemptRecord.id)
            )
            if result.scalar_one_or_none() is None:
                raise CallLedgerStateConflict(attempt_id, CallStatus.STARTED)
            await session.execute(
                update(ModelCallBudgetRecord)
                .where(ModelCallBudgetRecord.task_id == self.task_id)
                .values(**budget_values)
            )

    async def mark_failed(self, attempt_id: str, code: str, *, unknown: bool) -> None:
        now = utcnow()
        status = CallStatus.UNKNOWN if unknown else CallStatus.FAILED
        counter = (
            ModelCallBudgetRecord.unknown_calls
            if unknown
            else ModelCallBudgetRecord.failed_calls
        )
        counter_name = "unknown_calls" if unknown else "failed_calls"
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(ModelCallAttemptRecord)
                .where(
                    ModelCallAttemptRecord.id == attempt_id,
                    ModelCallAttemptRecord.task_id == self.task_id,
                    ModelCallAttemptRecord.status == CallStatus.STARTED.value,
                )
                .values(status=status.value, error_code=code, finished_at=now)
                .returning(ModelCallAttemptRecord.id)
            )
            if result.scalar_one_or_none() is None:
                raise CallLedgerStateConflict(attempt_id, CallStatus.STARTED)
            await session.execute(
                update(ModelCallBudgetRecord)
                .where(ModelCallBudgetRecord.task_id == self.task_id)
                .values(
                    **{
                        counter_name: counter + 1,
                        "usage_complete": False,
                        "updated_at": now,
                    }
                )
            )

    async def schedule_retry(self, attempt_id: str, kind: str, delay: float) -> None:
        policy = self._require_policy()
        now = utcnow()
        counter_name = "rate_limit_retries" if kind == "rate_limit" else "transient_retries"
        counter = (
            ModelCallBudgetRecord.rate_limit_retries
            if kind == "rate_limit"
            else ModelCallBudgetRecord.transient_retries
        )
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(ModelCallBudgetRecord)
                .where(
                    ModelCallBudgetRecord.task_id == self.task_id,
                    ModelCallBudgetRecord.backoff_seconds + delay
                    <= ModelCallBudgetRecord.max_total_backoff_seconds,
                )
                .values(
                    **{
                        "backoff_seconds": ModelCallBudgetRecord.backoff_seconds + delay,
                        counter_name: counter + 1,
                        "updated_at": now,
                    }
                )
                .returning(ModelCallBudgetRecord.backoff_seconds)
            )
            new_total = result.scalar_one_or_none()
            if new_total is None:
                current = await session.get(ModelCallBudgetRecord, self.task_id)
                if current is None:
                    raise KeyError(self.task_id)
                raise BackoffBudgetExceeded(
                    delay,
                    max(0.0, policy.max_total_backoff_seconds - current.backoff_seconds),
                )
            await session.execute(
                update(ModelCallAttemptRecord)
                .where(
                    ModelCallAttemptRecord.id == attempt_id,
                    ModelCallAttemptRecord.task_id == self.task_id,
                )
                .values(wait_seconds=delay)
            )

    async def snapshot(self) -> dict[str, int | float | bool | str | None]:
        async with self.database.sessions() as session:
            budget = await session.get(ModelCallBudgetRecord, self.task_id)
            if budget is None:
                raise KeyError(self.task_id)
            result = await session.execute(
                select(ModelCallAttemptRecord.status, ModelCallAttemptRecord.is_fallback).where(
                    ModelCallAttemptRecord.task_id == self.task_id
                )
            )
            rows = result.all()
        statuses = Counter(row.status for row in rows)
        complete = budget.succeeded_calls > 0 and budget.usage_complete
        return {
            "call_control_version": budget.policy_version,
            "model_calls": budget.reserved_calls,
            "reserved_calls": budget.reserved_calls,
            "started_calls": budget.started_calls,
            "successful_model_calls": budget.succeeded_calls,
            "failed_calls": budget.failed_calls,
            "unknown_calls": budget.unknown_calls,
            "pending_reserved_calls": statuses[CallStatus.RESERVED.value],
            "pending_started_calls": statuses[CallStatus.STARTED.value],
            "rate_limit_retries": budget.rate_limit_retries,
            "transient_retries": budget.transient_retries,
            "fallback_calls": sum(bool(row.is_fallback) for row in rows),
            "max_model_calls": budget.max_calls,
            "backoff_seconds": budget.backoff_seconds,
            "max_total_backoff_seconds": budget.max_total_backoff_seconds,
            "observed_input_tokens": budget.observed_input_tokens,
            "observed_output_tokens": budget.observed_output_tokens,
            "observed_total_tokens": budget.observed_total_tokens,
            "input_tokens": budget.observed_input_tokens if complete else None,
            "output_tokens": budget.observed_output_tokens if complete else None,
            "total_tokens": budget.observed_total_tokens if complete else None,
            "token_usage_complete": complete,
            "max_total_tokens": budget.max_total_tokens,
            "max_output_tokens": budget.max_output_tokens,
        }
