from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from repopilot.candidates import Candidate, digest_json
from repopilot.models import SandboxResult, TaskStatus

Digest = str


class TestEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["test-evidence-v1"] = "test-evidence-v1"
    evidence_digest: str
    candidate_id: str
    artifact_digest: str
    image_id: str
    command: list[str]
    policy: dict[str, Any]
    result: SandboxResult
    observed_at: datetime
    scope: Literal["configured-tests"] = "configured-tests"


def evidence_identity(evidence: TestEvidence) -> str:
    return digest_json(evidence.model_dump(mode="json", exclude={"evidence_digest"}))


class ApprovalRequest(BaseModel):
    """Required binding is intentional: legacy boolean-only requests return 422."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["approval-v2"]
    approval_id: UUID
    request_version: int = Field(ge=1)
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved: StrictBool
    feedback: str = Field(default="", max_length=2_000)


class ApprovalDecision(BaseModel):
    decision_id: str
    approval_id: str
    task_id: str
    request_version: int
    candidate_id: str
    evidence_digest: str
    approved: bool
    feedback: str
    actor: str
    decided_at: datetime
    dispatch_state: Literal["pending", "enqueued", "consumed", "blocked"]
    dispatch_error: str | None = None

    def resume_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "approval-v2",
            "decision_id": self.decision_id,
            "approval_id": self.approval_id,
            "request_version": self.request_version,
            "candidate_id": self.candidate_id,
            "evidence_digest": self.evidence_digest,
            "approved": self.approved,
            "feedback": self.feedback,
        }


class ApprovalView(BaseModel):
    schema_version: Literal["approval-v2"] = "approval-v2"
    approval_id: str
    task_id: str
    request_version: int
    status: str
    expires_at: datetime
    candidate: Candidate
    evidence: TestEvidence
    review: dict[str, Any]
    decision: ApprovalDecision | None = None


class ApprovalReceipt(BaseModel):
    saved: Literal[True] = True
    task_id: str
    task_status: TaskStatus
    decision: ApprovalDecision


class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: UUID
