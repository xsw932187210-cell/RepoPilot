"""Durable candidate/evidence bindings and single-consumer approval decisions.

The decision and resume intent commit together with the task CAS. Redis is only a
delivery attempt; until CH-04A adds an outbox dispatcher, operators explicitly retry
saved pending/enqueued intents through the API. No queue acknowledgement is claimed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from repopilot.approval_models import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalView,
    TestEvidence,
    evidence_identity,
)
from repopilot.candidates import Candidate, candidate_identity
from repopilot.db import (
    Base,
    EventRecord,
    TaskRecord,
    TaskStateConflict,
    phase_transition_values,
    utcnow,
)
from repopilot.models import TASK_RESULT_SCHEMA_VERSION, TaskStatus, TaskView
from repopilot.task_timing import remaining_seconds

if TYPE_CHECKING:
    from repopilot.db import Database


class ApprovalConflict(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class CandidateRecord(Base):
    __tablename__ = "candidates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class EvidenceRecord(Base):
    __tablename__ = "test_evidence"

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ApprovalRecord(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (UniqueConstraint("task_id", "request_version", name="uq_approval_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    request_version: Mapped[int] = mapped_column(Integer)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"))
    evidence_digest: Mapped[str] = mapped_column(ForeignKey("test_evidence.digest"))
    status: Mapped[str] = mapped_column(String(24), default="waiting")
    review: Mapped[dict[str, Any]] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DecisionRecord(Base):
    __tablename__ = "approval_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey("approval_requests.id"), unique=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    dispatch_state: Mapped[str] = mapped_column(String(24), default="pending")
    dispatch_error: Mapped[str | None] = mapped_column(String(80), nullable=True)

    def to_view(self) -> ApprovalDecision:
        return ApprovalDecision.model_validate(
            {
                **self.payload,
                "dispatch_state": self.dispatch_state,
                "dispatch_error": self.dispatch_error,
            }
        )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class ApprovalStore:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _validate_evidence(task_id: str, candidate: Candidate, evidence: TestEvidence) -> None:
        if (
            candidate.task_id != task_id
            or candidate_identity(candidate) != candidate.candidate_id
            or evidence_identity(evidence) != evidence.evidence_digest
            or evidence.candidate_id != candidate.candidate_id
            or evidence.artifact_digest != candidate.artifact_digest
            or evidence.command != evidence.result.command
            or evidence.image_id != evidence.result.image_id
        ):
            raise ApprovalConflict("invalid_candidate_evidence_binding")

    @staticmethod
    async def _save_evidence(
        session: AsyncSession,
        task_id: str,
        candidate: Candidate,
        evidence: TestEvidence,
    ) -> None:
        existing = await session.get(CandidateRecord, candidate.candidate_id)
        payload = candidate.model_dump(mode="json")
        if existing is None:
            session.add(
                CandidateRecord(id=candidate.candidate_id, task_id=task_id, payload=payload)
            )
            await session.flush()
        elif existing.payload != payload:
            raise ApprovalConflict("candidate_identity_collision")
        saved = await session.get(EvidenceRecord, evidence.evidence_digest)
        payload = evidence.model_dump(mode="json")
        if saved is None:
            session.add(
                EvidenceRecord(
                    digest=evidence.evidence_digest,
                    task_id=task_id,
                    candidate_id=candidate.candidate_id,
                    payload=payload,
                )
            )
            await session.flush()
        elif saved.payload != payload:
            raise ApprovalConflict("evidence_identity_collision")

    async def record_evidence(
        self,
        task_id: str,
        candidate: Candidate,
        evidence: TestEvidence,
    ) -> None:
        self._validate_evidence(task_id, candidate, evidence)
        async with self.database.sessions() as session, session.begin():
            from repopilot.generations import require_current

            await require_current(session, task_id, candidate.candidate_id)
            await self._save_evidence(session, task_id, candidate, evidence)

    async def open_request(
        self,
        task_id: str,
        *,
        expected_version: int,
        candidate: Candidate,
        evidence: TestEvidence,
        summary: dict[str, Any],
        ttl_seconds: int = 86_400,
    ) -> TaskView:
        self._validate_evidence(task_id, candidate, evidence)
        if not evidence.result.passed:
            raise ApprovalConflict("invalid_candidate_evidence_binding")
        approval_id = str(uuid.uuid4())
        version = expected_version + 1
        summary = {**summary, "approval_id": approval_id, "approval_request_version": version}
        async with self.database.sessions() as session:
            from repopilot.generations import require_current

            await require_current(session, task_id, candidate.candidate_id)
            task = await session.get(TaskRecord, task_id)
            result = await session.execute(
                update(TaskRecord)
                .where(
                    TaskRecord.id == task_id,
                    TaskRecord.status == TaskStatus.RUNNING.value,
                    TaskRecord.state_version == expected_version,
                )
                .values(
                    status=TaskStatus.AWAITING_APPROVAL.value,
                    state_version=version,
                    result=summary,
                    result_schema_version=TASK_RESULT_SCHEMA_VERSION,
                    updated_at=utcnow(),
                    **phase_transition_values(task, utcnow()),
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                current = await session.get(TaskRecord, task_id)
                if current is None:
                    raise KeyError(task_id)
                raise TaskStateConflict(
                    task_id,
                    expected_status=TaskStatus.RUNNING,
                    expected_version=expected_version,
                    current_status=TaskStatus(current.status),
                    current_version=current.state_version,
                )
            # A newer request supersedes any older unconsumed request, never its decision.
            await session.execute(
                update(ApprovalRecord)
                .where(
                    ApprovalRecord.task_id == task_id,
                    ApprovalRecord.status == "waiting",
                )
                .values(status="superseded")
            )
            await self._save_evidence(session, task_id, candidate, evidence)
            session.add(
                ApprovalRecord(
                    id=approval_id,
                    task_id=task_id,
                    request_version=version,
                    candidate_id=candidate.candidate_id,
                    evidence_digest=evidence.evidence_digest,
                    status="waiting",
                    review=summary.get("review") or {},
                    expires_at=utcnow() + timedelta(seconds=ttl_seconds),
                )
            )
            await session.commit()
        task = await self.database.get_task(task_id)
        assert task is not None
        return task

    async def current(self, task_id: str) -> ApprovalView:
        async with self.database.sessions() as session:
            task = await session.get(TaskRecord, task_id)
            if task is None:
                raise KeyError(task_id)
            row = (
                await session.execute(
                    select(ApprovalRecord)
                    .where(
                        ApprovalRecord.task_id == task_id,
                    )
                    .order_by(ApprovalRecord.request_version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                code = (
                    "legacy_approval_requires_revalidation"
                    if task.status == TaskStatus.AWAITING_APPROVAL.value
                    else "no_approval_request"
                )
                raise ApprovalConflict(code)
            candidate = await session.get(CandidateRecord, row.candidate_id)
            evidence = await session.get(EvidenceRecord, row.evidence_digest)
            decision = (
                await session.execute(
                    select(DecisionRecord).where(
                        DecisionRecord.approval_id == row.id,
                    )
                )
            ).scalar_one_or_none()
            assert candidate is not None and evidence is not None
            status = row.status
            if status == "waiting" and _aware(row.expires_at) <= utcnow():
                status = "expired"
            return ApprovalView(
                approval_id=row.id,
                task_id=task_id,
                request_version=row.request_version,
                status=status,
                expires_at=_aware(row.expires_at),
                candidate=candidate.payload,
                evidence=evidence.payload,
                review=row.review,
                decision=decision.to_view() if decision else None,
            )

    async def candidate(self, task_id: str, candidate_id: str) -> Candidate:
        async with self.database.sessions() as session:
            row = await session.get(CandidateRecord, candidate_id)
            if row is None or row.task_id != task_id:
                raise KeyError(candidate_id)
            return Candidate.model_validate(row.payload)

    async def decide(
        self,
        task_id: str,
        request: ApprovalRequest,
        *,
        actor: str,
    ) -> ApprovalDecision:
        async with self.database.sessions() as session:
            # Serialize with other decisions on PostgreSQL. The conditional task update
            # remains authoritative on SQLite, where FOR UPDATE is not supported.
            task = (
                await session.execute(
                    select(TaskRecord)
                    .where(
                        TaskRecord.id == task_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if task is None:
                raise KeyError(task_id)
            row = await session.get(ApprovalRecord, str(request.approval_id))
            if row is None or row.task_id != task_id:
                raise ApprovalConflict("unknown_approval_request")
            if (
                row.request_version != request.request_version
                or row.candidate_id != request.candidate_id
                or row.evidence_digest != request.evidence_digest
            ):
                raise ApprovalConflict("stale_approval_binding")
            existing = (
                await session.execute(
                    select(DecisionRecord).where(
                        DecisionRecord.approval_id == row.id,
                    )
                )
            ).scalar_one_or_none()
            if existing:
                saved = existing.to_view()
                if (saved.approved, saved.feedback, saved.actor) == (
                    request.approved,
                    request.feedback,
                    actor,
                ):
                    return saved
                raise ApprovalConflict("approval_already_decided")
            if row.status != "waiting":
                raise ApprovalConflict(f"approval_{row.status}")
            if _aware(row.expires_at) <= utcnow():
                row.status = "expired"
                await session.commit()
                raise ApprovalConflict("approval_expired")
            if task.status != TaskStatus.AWAITING_APPROVAL.value:
                raise ApprovalConflict("task_not_awaiting_approval")
            if task.state_version != request.request_version:
                raise ApprovalConflict("stale_approval_version")
            if remaining_seconds(task, utcnow()) == 0:
                raise ApprovalConflict("task_deadline_exceeded")
            from repopilot.generations import require_current

            await require_current(session, task_id, row.candidate_id)
            decision = ApprovalDecision(
                decision_id=str(uuid.uuid4()),
                approval_id=row.id,
                task_id=task_id,
                request_version=row.request_version,
                candidate_id=row.candidate_id,
                evidence_digest=row.evidence_digest,
                approved=request.approved,
                feedback=request.feedback,
                actor=actor,
                decided_at=utcnow(),
                dispatch_state="pending",
            )
            result = await session.execute(
                update(TaskRecord)
                .where(
                    TaskRecord.id == task_id,
                    TaskRecord.status == TaskStatus.AWAITING_APPROVAL.value,
                    TaskRecord.state_version == request.request_version,
                )
                .values(
                    status=TaskStatus.QUEUED.value,
                    state_version=request.request_version + 1,
                    result={**(task.result or {}), "approval_decision_id": decision.decision_id},
                    updated_at=utcnow(),
                    **phase_transition_values(task, utcnow()),
                )
            )
            if result.rowcount != 1:
                raise ApprovalConflict("approval_state_race")
            row.status = "decided"
            session.add(
                DecisionRecord(
                    id=decision.decision_id,
                    approval_id=row.id,
                    task_id=task_id,
                    payload=decision.model_dump(
                        mode="json", exclude={"dispatch_state", "dispatch_error"}
                    ),
                    dispatch_state="pending",
                )
            )
            session.add(
                EventRecord(
                    task_id=task_id,
                    kind="human_decision",
                    node="api",
                    message="Human decision and resume intent saved",
                    payload={
                        "decision_id": decision.decision_id,
                        "approved": decision.approved,
                        "candidate_id": decision.candidate_id,
                        "evidence_digest": row.evidence_digest,
                    },
                )
            )
            await session.commit()
            return decision

    async def decision(self, task_id: str, decision_id: str) -> ApprovalDecision:
        async with self.database.sessions() as session:
            row = await session.get(DecisionRecord, decision_id)
            if row is None or row.task_id != task_id:
                raise ApprovalConflict("unknown_approval_decision")
            return row.to_view()

    async def dispatchable(self, task_id: str, decision_id: str) -> ApprovalDecision:
        decision = await self.decision(task_id, decision_id)
        task = await self.database.get_task(task_id)
        if (
            task is None
            or task.status != TaskStatus.QUEUED
            or task.state_version != decision.request_version + 1
            or decision.dispatch_state not in {"pending", "enqueued"}
        ):
            raise ApprovalConflict("approval_resume_not_dispatchable")
        return decision

    async def delivery_attempt(self, decision_id: str, *, succeeded: bool) -> None:
        async with self.database.sessions() as session, session.begin():
            await session.execute(
                update(DecisionRecord)
                .where(
                    DecisionRecord.id == decision_id,
                    DecisionRecord.dispatch_state.in_(["pending", "enqueued"]),
                )
                .values(
                    dispatch_state="enqueued" if succeeded else "pending",
                    dispatch_error=None if succeeded else "queue_delivery_failed",
                )
            )

    async def resume_for_running_task(self, task: TaskView) -> dict | None:
        decision_id = (task.result or {}).get("approval_decision_id")
        if not decision_id:
            return None
        decision = await self.decision(task.id, decision_id)
        if task.state_version != decision.request_version + 2:
            raise ApprovalConflict("stale_approval_resume")
        async with self.database.sessions() as session, session.begin():
            from repopilot.generations import require_current

            await require_current(session, task.id, decision.candidate_id)
            outcome = await session.execute(
                update(DecisionRecord)
                .where(
                    DecisionRecord.id == decision_id,
                    DecisionRecord.dispatch_state.in_(["pending", "enqueued"]),
                )
                .values(dispatch_state="consumed", dispatch_error=None)
            )
            if outcome.rowcount != 1:
                raise ApprovalConflict("approval_resume_already_consumed")
        return decision.resume_payload()

    async def validate_resume(self, task_id: str, resume: dict) -> ApprovalDecision:
        decision = await self.decision(task_id, str(resume.get("decision_id", "")))
        task = await self.database.get_task(task_id)
        current = await self.current(task_id)
        if (
            task is None
            or task.status != TaskStatus.RUNNING
            or task.state_version != decision.request_version + 2
            or current.approval_id != decision.approval_id
            or decision.dispatch_state != "consumed"
            or resume != decision.resume_payload()
        ):
            raise ApprovalConflict("stale_approval_resume")
        from repopilot.generations import SQLGenerationStore

        await SQLGenerationStore(self.database).assert_current(current.candidate)
        # Only an approval interrupt resumed by this protocol may start a new publication.
        # Old already-approved/finalizing checkpoints may have sent untracked HTTP requests.
        async with self.database.sessions() as session, session.begin():
            row = await session.get(DecisionRecord, decision.decision_id)
            row.payload = {**row.payload, "publication_protocol_version": "publish-intent-v1"}
        return decision.model_copy(update={"publication_protocol_version": "publish-intent-v1"})
