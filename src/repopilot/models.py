from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from repopilot.security import CapabilityReason


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TASK_RESULT_SCHEMA_VERSION = 1
EVENT_PAYLOAD_SCHEMA_VERSION = 1

ALLOWED_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.CANCELLING, TaskStatus.FAILED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.AWAITING_APPROVAL,
            TaskStatus.CANCELLING,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.AWAITING_APPROVAL: frozenset(
        {TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.CANCELLING, TaskStatus.FAILED}
    ),
    TaskStatus.CANCELLING: frozenset({TaskStatus.CANCELLED, TaskStatus.FAILED}),
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

    execution_timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    deadline_includes_queue: bool | None = None
    deadline_includes_approval: bool | None = None

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
    execution_timeout_seconds: int | None = None
    deadline_includes_queue: bool = False
    deadline_includes_approval: bool = False
    execution_elapsed_seconds: float = 0
    queue_elapsed_seconds: float = 0
    approval_elapsed_seconds: float = 0
    phase_started_at: datetime | None = None
    deadline_at: datetime | None = None
    remaining_seconds: float | None = None
    stop_requested_at: datetime | None = None
    stopped_at: datetime | None = None
    stop_reason: str | None = None
    execution_resources: list[dict[str, Any]] = Field(default_factory=list)
    cleanup_pending: list[dict[str, Any]] = Field(default_factory=list)


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
    action: Literal["write", "create"] = "write"
    # Optional for old artifacts and direct tool callers; the graph requires this
    # precondition for every model-proposed write before touching the workspace.
    expected_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CodeChangeOutput(BaseModel):
    summary: str
    edits: list[FileEdit] = Field(default_factory=list, max_length=8)


class PolicyFeedback(BaseModel):
    schema_version: Literal["policy-feedback-v1"] = "policy-feedback-v1"
    reason_code: CapabilityReason
    target_path: str | None = None
    allowed_actions: list[Literal["write"]] = Field(default_factory=lambda: ["write"])
    allowed_paths: list[str] = Field(default_factory=list, max_length=32)
    guidance: str


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
    image_id: str = "unreported-test-runner"
    policy_version: str = "sandbox-v1"

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
    model_call_policy_version: str | None = None
    model_calls_max: int = Field(default=0, ge=0)
    model_calls_reserved: int = Field(default=0, ge=0)
    model_calls_started: int = Field(default=0, ge=0)
    model_calls_succeeded: int = Field(default=0, ge=0)
    model_calls_failed: int = Field(default=0, ge=0)
    model_calls_unknown: int = Field(default=0, ge=0)
    model_calls_pending_reservation: int = Field(default=0, ge=0)
    model_calls_pending_response: int = Field(default=0, ge=0)
    model_rate_limit_retries: int = Field(default=0, ge=0)
    model_transient_retries: int = Field(default=0, ge=0)
    model_fallback_calls: int = Field(default=0, ge=0)
    model_backoff_seconds: float = Field(default=0, ge=0)
    model_token_usage_complete: bool = False
    model_observed_input_tokens: int = Field(default=0, ge=0)
    model_observed_output_tokens: int = Field(default=0, ge=0)
    model_observed_total_tokens: int = Field(default=0, ge=0)
