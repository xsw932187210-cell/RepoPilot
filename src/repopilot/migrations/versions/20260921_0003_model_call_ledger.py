"""Add CH-10's persistent model-call budget and attempt ledger."""

import sqlalchemy as sa
from alembic import op

revision = "20260921_0003"
down_revision = "20260921_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_call_budgets",
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("policy_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("max_calls", sa.Integer(), nullable=False),
        sa.Column("request_timeout_seconds", sa.Float(), nullable=False),
        sa.Column("max_rate_limit_retries", sa.Integer(), nullable=False),
        sa.Column("max_transient_retries", sa.Integer(), nullable=False),
        sa.Column("base_backoff_seconds", sa.Float(), nullable=False),
        sa.Column("max_retry_wait_seconds", sa.Float(), nullable=False),
        sa.Column("max_total_backoff_seconds", sa.Float(), nullable=False),
        sa.Column("max_total_tokens", sa.Integer(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("succeeded_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unknown_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rate_limit_retries", sa.Integer(), server_default="0", nullable=False),
        sa.Column("transient_retries", sa.Integer(), server_default="0", nullable=False),
        sa.Column("backoff_seconds", sa.Float(), server_default="0", nullable=False),
        sa.Column("observed_input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("observed_output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("observed_total_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("usage_complete", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_table(
        "model_call_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("logical_call_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("adapter_version", sa.String(length=100), nullable=False),
        sa.Column("request_schema_version", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("retry_index", sa.Integer(), nullable=False),
        sa.Column("is_fallback", sa.Boolean(), nullable=False),
        sa.Column("fallback_from_provider", sa.String(length=80), nullable=True),
        sa.Column("fallback_from_model", sa.String(length=200), nullable=True),
        sa.Column("wait_seconds", sa.Float(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["task_id"], ["model_call_budgets.task_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id", "sequence_no", name="uq_model_call_attempt_task_sequence"
        ),
    )
    op.create_index(
        "ix_model_call_attempts_logical_call_id",
        "model_call_attempts",
        ["logical_call_id"],
        unique=False,
    )
    op.create_index(
        "ix_model_call_attempts_status",
        "model_call_attempts",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_model_call_attempts_task_id",
        "model_call_attempts",
        ["task_id"],
        unique=False,
    )


def downgrade() -> None:
    raise RuntimeError(
        "Destructive schema downgrades are unsupported; restore a backup or apply a forward fix"
    )
