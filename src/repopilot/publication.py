"""Durable publication authority and receipts; no external I/O in the store."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, ForeignKey, Integer, String, select, update
from sqlalchemy.orm import Mapped, mapped_column

from repopilot.approval_models import TestEvidence
from repopilot.approvals import (
    ApprovalRecord,
    ApprovalStore,
    CandidateRecord,
    DecisionRecord,
    EvidenceRecord,
)
from repopilot.candidates import ArtifactStore, Candidate, digest_json
from repopilot.db import Base, TaskRecord
from repopilot.generations import require_current
from repopilot.models import TaskStatus
from repopilot.security import normalize_workspace_path, validate_branch, validate_repository_url

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from repopilot.db import Database


class PublicationConflict(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class PublishIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["publish-intent-v1"] = "publish-intent-v1"
    publish_intent_id: str
    task_id: str
    task_version: int
    decision_id: str
    candidate_id: str
    evidence_digest: str
    target_repo: str
    base_branch: str
    approved_base_sha: str
    branch: str
    title: str
    commit_date: datetime
    tree_sha: str
    commit_sha: str

    @property
    def message(self) -> str:
        return f"RepoPilot approved candidate\n\nPublish-Intent: {self.publish_intent_id}\n"

    @property
    def body(self) -> str:
        return (
            "Generated after configured tests and human approval.\n\n"
            f"<!-- repopilot-intent:{self.publish_intent_id} -->\n"
            f"Task: {self.task_id}\nDecision: {self.decision_id}\n"
            f"Candidate: {self.candidate_id}\nEvidence: {self.evidence_digest}\n"
            f"Approved base: {self.approved_base_sha}\n"
        )


class PublishReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["publish-receipt-v1"] = "publish-receipt-v1"
    publish_intent_id: str
    branch: str
    version: int = 0
    phase: Literal[
        "pending",
        "publishing",
        "unknown",
        "published",
        "blocked",
        "cancelled",
        "revalidation_required",
    ] = "pending"
    step: str | None = None
    steps: dict[str, str] = Field(default_factory=dict)
    remote_ids: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    cancel_requested: bool = False
    requires_revalidation: bool = False
    observed_base_shas: list[str] = Field(default_factory=list)

    @property
    def published(self) -> bool:
        return "pr_number" in self.remote_ids

    @property
    def pull_request_url(self) -> str | None:
        return self.remote_ids.get("pull_request_url")


class PublishIntentRecord(Base):
    __tablename__ = "publish_intents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    decision_id: Mapped[str] = mapped_column(ForeignKey("approval_decisions.id"), unique=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    payload_digest: Mapped[str] = mapped_column(String(64))
    receipt: Mapped[dict[str, Any]] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=0)


def git_oid(kind: str, data: bytes) -> str:
    # Git SHA-1 is an object identifier, not an authorization or artifact digest.
    return hashlib.sha1(  # noqa: S324 -- required by Git's object format.
        kind.encode() + b" " + str(len(data)).encode() + b"\0" + data
    ).hexdigest()


def git_tree(entries: list[dict[str, str]]) -> str:
    tree: dict[str, Any] = {}
    for entry in entries:
        components = entry["path"].split("/")
        node = tree
        for part in components[:-1]:
            node = node.setdefault(part, {})
        node[components[-1]] = (entry["mode"], entry["sha"])

    def encode(node: dict) -> str:
        data = bytearray()
        for name, value in sorted(
            node.items(),
            key=lambda item: (item[0] + ("/" if isinstance(item[1], dict) else "")).encode(),
        ):
            mode, sha = ("40000", encode(value)) if isinstance(value, dict) else value
            data.extend(mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(sha))
        return git_oid("tree", bytes(data))

    return encode(tree)


def commit_payload(intent: PublishIntent) -> dict[str, Any]:
    identity = {
        "name": "RepoPilot",
        "email": "repopilot@users.noreply.github.com",
        "date": intent.commit_date.astimezone(UTC).isoformat(timespec="seconds"),
    }
    return {
        "message": intent.message,
        "tree": intent.tree_sha,
        "parents": [intent.approved_base_sha],
        "author": identity,
        "committer": identity,
    }


def commit_oid(intent: PublishIntent) -> str:
    person = (
        "RepoPilot <repopilot@users.noreply.github.com> "
        f"{int(intent.commit_date.timestamp())} +0000"
    )
    data = (
        f"tree {intent.tree_sha}\nparent {intent.approved_base_sha}\n"
        f"author {person}\ncommitter {person}\n\n{intent.message}"
    ).encode()
    return git_oid("commit", data)


def artifact_objects(
    artifacts: ArtifactStore,
    candidate: Candidate,
) -> tuple[list[dict[str, str]], dict[str, bytes]]:
    artifacts.read_diff(candidate)
    entries, blobs = [], {}
    with artifacts.materialize(candidate) as root:
        for item in candidate.manifest["entries"]:
            if item["kind"] == "directory":
                continue
            path = item["path"]
            if normalize_workspace_path(path).value != path or item["mode"] not in {0o644, 0o755}:
                raise PublicationConflict("unsupported_publication_file")
            data = (root / path).read_bytes()
            sha = git_oid("blob", data)
            blobs[sha] = data
            entries.append(
                {"path": path, "mode": f"100{item['mode']:o}", "type": "blob", "sha": sha}
            )
    if not entries:
        raise PublicationConflict("empty_publication_tree")
    return entries, blobs


class PublicationStore:
    def __init__(self, database: Database, artifacts: ArtifactStore):
        self.database, self.artifacts = database, artifacts

    async def _binding(
        self,
        session: AsyncSession,
        task_id: str,
        decision_id: str,
        candidate_id: str,
        *,
        active: bool,
    ) -> tuple[TaskRecord, Any, Candidate]:
        task = (
            await session.execute(
                select(TaskRecord).where(TaskRecord.id == task_id).with_for_update()
            )
        ).scalar_one_or_none()
        row = await session.get(DecisionRecord, decision_id)
        if task is None or row is None or row.task_id != task_id:
            raise PublicationConflict("publication_authority_missing")
        decision = row.to_view()
        approval = await session.get(ApprovalRecord, decision.approval_id)
        candidate_row = await session.get(CandidateRecord, candidate_id)
        evidence_row = await session.get(EvidenceRecord, decision.evidence_digest)
        if approval is None or candidate_row is None or evidence_row is None:
            raise PublicationConflict("publication_authority_missing")
        candidate = Candidate.model_validate(candidate_row.payload)
        evidence = TestEvidence.model_validate(evidence_row.payload)
        ApprovalStore._validate_evidence(task_id, candidate, evidence)
        latest = (
            await session.execute(
                select(ApprovalRecord.id)
                .where(ApprovalRecord.task_id == task_id)
                .order_by(ApprovalRecord.request_version.desc())
                .limit(1)
            )
        ).scalar_one()
        expires = (
            approval.expires_at.replace(tzinfo=UTC)
            if (approval.expires_at.tzinfo is None)
            else approval.expires_at
        )
        if (
            not decision.approved
            or decision.publication_protocol_version != "publish-intent-v1"
            or decision.dispatch_state != "consumed"
            or approval.status != "decided"
            or latest != approval.id
            or approval.task_id != task_id
            or decision.task_id != task_id
            or candidate_row.task_id != task_id
            or evidence_row.task_id != task_id
            or approval.candidate_id != candidate_id
            or decision.candidate_id != candidate_id
            or approval.request_version != decision.request_version
            or approval.evidence_digest != decision.evidence_digest
            or decision.decided_at > expires
            or not evidence.result.passed
            or (task.result or {}).get("approval_decision_id") != decision_id
        ):
            raise PublicationConflict("invalid_publication_approval")
        if active:
            if task.status != TaskStatus.RUNNING.value or (
                task.state_version != decision.request_version + 2
            ):
                raise PublicationConflict("publication_task_stopped")
            await require_current(session, task_id, candidate_id)
        return task, decision, candidate

    async def ensure(self, task_id: str, decision_id: str, candidate_id: str) -> PublishIntent:
        async with self.database.sessions() as session, session.begin():
            task, decision, candidate = await self._binding(
                session,
                task_id,
                decision_id,
                candidate_id,
                active=True,
            )
            owner, repo = validate_repository_url(task.repository_url)
            validate_branch(task.base_branch)
            if len(candidate.base_sha) != 40:
                raise PublicationConflict("unsupported_github_object_format")
            entries, _ = artifact_objects(self.artifacts, candidate)
            identity = digest_json(
                {
                    "protocol": "publish-intent-v1",
                    "task_id": task_id,
                    "decision_id": decision_id,
                    "candidate_id": candidate_id,
                    "target_repo": f"{owner}/{repo}".lower(),
                }
            )
            intent = PublishIntent(
                publish_intent_id=identity,
                task_id=task_id,
                task_version=task.state_version,
                decision_id=decision_id,
                candidate_id=candidate_id,
                evidence_digest=decision.evidence_digest,
                target_repo=f"{owner}/{repo}".lower(),
                base_branch=task.base_branch,
                approved_base_sha=candidate.base_sha,
                branch=f"repopilot/{task_id}/{identity}",
                title=f"[RepoPilot] {task.issue_title}",
                commit_date=decision.decided_at.astimezone(UTC).replace(microsecond=0),
                tree_sha=git_tree(entries),
                commit_sha="",
            )
            intent = intent.model_copy(update={"commit_sha": commit_oid(intent)})
            existing = await session.get(PublishIntentRecord, identity)
            if existing:
                if existing.payload != intent.model_dump(mode="json"):
                    raise PublicationConflict("publication_intent_conflict")
            else:
                receipt = PublishReceipt(publish_intent_id=identity, branch=intent.branch)
                session.add(
                    PublishIntentRecord(
                        id=identity,
                        task_id=task_id,
                        decision_id=decision_id,
                        candidate_id=candidate_id,
                        payload=intent.model_dump(mode="json"),
                        payload_digest=digest_json(intent.model_dump(mode="json")),
                        receipt=receipt.model_dump(mode="json"),
                        version=0,
                    )
                )
            return intent

    async def load(self, intent_id: str) -> tuple[PublishIntent, PublishReceipt, Candidate]:
        async with self.database.sessions() as session:
            row = await session.get(PublishIntentRecord, intent_id)
            if row is None:
                raise PublicationConflict("unknown_publish_intent")
            intent = PublishIntent.model_validate(row.payload)
            receipt = PublishReceipt.model_validate(row.receipt)
            candidate = await session.get(CandidateRecord, row.candidate_id)
            if (
                intent.publish_intent_id != row.id
                or digest_json(row.payload) != row.payload_digest
                or intent.task_id != row.task_id
                or intent.decision_id != row.decision_id
                or intent.candidate_id != row.candidate_id
                or receipt.publish_intent_id != row.id
                or receipt.branch != intent.branch
                or receipt.version != row.version
                or candidate is None
            ):
                raise PublicationConflict("corrupt_publish_intent")
            return intent, receipt, Candidate.model_validate(candidate.payload)

    async def authorize(self, intent: PublishIntent) -> None:
        expected = await self.ensure(intent.task_id, intent.decision_id, intent.candidate_id)
        if expected != intent:
            raise PublicationConflict("publication_intent_conflict")

    async def save(self, receipt: PublishReceipt) -> PublishReceipt:
        saved = receipt.model_copy(update={"version": receipt.version + 1}, deep=True)
        async with self.database.sessions() as session, session.begin():
            result = await session.execute(
                update(PublishIntentRecord)
                .where(
                    PublishIntentRecord.id == receipt.publish_intent_id,
                    PublishIntentRecord.version == receipt.version,
                )
                .values(receipt=saved.model_dump(mode="json"), version=saved.version)
            )
            if result.rowcount != 1:
                raise PublicationConflict("stale_publication_receipt")
        return saved

    async def receipts(self, task_id: str) -> list[PublishReceipt]:
        async with self.database.sessions() as session:
            rows = (
                await session.execute(
                    select(PublishIntentRecord)
                    .where(
                        PublishIntentRecord.task_id == task_id,
                    )
                    .order_by(PublishIntentRecord.id)
                )
            ).scalars()
            return [PublishReceipt.model_validate(row.receipt) for row in rows]

    async def for_decision(self, task_id: str, decision_id: str) -> str | None:
        async with self.database.sessions() as session:
            return await session.scalar(
                select(PublishIntentRecord.id).where(
                    PublishIntentRecord.task_id == task_id,
                    PublishIntentRecord.decision_id == decision_id,
                )
            )
