from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TASK_RESULT_SCHEMA_VERSION = 1
EVENT_PAYLOAD_SCHEMA_VERSION = 1

ALLOWED_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.AWAITING_APPROVAL,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.AWAITING_APPROVAL: frozenset(
        {TaskStatus.QUEUED, TaskStatus.CANCELLED}
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


class TaskCreate(BaseModel):
    repository_url: str = Field(
        description="A public GitHub HTTPS URL or an allowlisted bundled demo URI."
    )
    issue_title: str = Field(min_length=3, max_length=240)
    issue_body: str = Field(min_length=3, max_length=12_000)
    base_branch: str = Field(default="main", min_length=1, max_length=120)
    test_command: str = Field(default="python -m pytest -q", max_length=300)
    max_iterations: int = Field(default=2, ge=1, le=5)

    @field_validator("repository_url")
    @classmethod
    def strip_repository_url(cls, value: str) -> str:
        return value.strip()


class TaskView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: TaskStatus
    repository_url: str
    issue_title: str
    issue_body: str
    base_branch: str
    test_command: str
    max_iterations: int
    graph_thread_id: str
    state_version: int = Field(ge=1)
    result: dict[str, Any] | None = None
    result_schema_version: int = Field(ge=0)
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class ApprovalRequest(BaseModel):
    approved: bool
    feedback: str = Field(default="", max_length=2_000)


class EventView(BaseModel):
    id: int
    task_id: str
    kind: str
    node: str
    message: str
    payload: dict[str, Any]
    payload_schema_version: int = Field(ge=0)
    created_at: datetime


class PlanOutput(BaseModel):
    summary: str
    steps: list[str] = Field(min_length=1, max_length=8)
    search_terms: list[str] = Field(default_factory=list, max_length=12)
    risk_notes: list[str] = Field(default_factory=list, max_length=8)


class FileEdit(BaseModel):
    path: str = Field(min_length=1, max_length=300)
    content: str = Field(max_length=200_000)
    reason: str = Field(default="", max_length=1_000)


class CodeChangeOutput(BaseModel):
    summary: str
    edits: list[FileEdit] = Field(default_factory=list, max_length=8)


class ReviewOutput(BaseModel):
    approved: bool
    summary: str
    feedback: list[str] = Field(default_factory=list, max_length=10)
    risk_level: str = Field(default="low", pattern="^(low|medium|high)$")


class SandboxResult(BaseModel):
    command: list[str]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return not self.timed_out and self.exit_code == 0


class NodeMetric(BaseModel):
    node: str
    duration_ms: int = Field(ge=0)
    iteration: int = Field(default=0, ge=0)


class TaskMetrics(BaseModel):
    task_id: str
    status: TaskStatus
    wall_time_ms: int = Field(ge=0)
    node_time_ms: int = Field(ge=0)
    node_runs: dict[str, int]
    node_duration_ms: dict[str, int]
    iterations: int = Field(ge=0)
    sandbox_time_ms: int = Field(ge=0)
    retrieval_strategy: str | None = None
    retrieval_candidate_files: int = Field(default=0, ge=0)
    retrieval_selected_files: int = Field(default=0, ge=0)
    retrieval_context_chars: int = Field(default=0, ge=0)
