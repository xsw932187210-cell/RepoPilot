"""Persist immutable candidate references, evidence and one-time approval intents."""

import sqlalchemy as sa
from alembic import op

revision = "20261019_0004"
down_revision = "20260921_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "candidates",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index("ix_candidates_task_id", "candidates", ["task_id"])
    op.create_table(
        "test_evidence",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column(
            "task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("candidate_id", sa.String(64), sa.ForeignKey("candidates.id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_index("ix_test_evidence_task_id", "test_evidence", ["task_id"])
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("request_version", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.String(64), sa.ForeignKey("candidates.id"), nullable=False),
        sa.Column(
            "evidence_digest", sa.String(64), sa.ForeignKey("test_evidence.digest"), nullable=False
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("review", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "request_version", name="uq_approval_version"),
    )
    op.create_index("ix_approval_requests_task_id", "approval_requests", ["task_id"])
    op.create_table(
        "approval_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "approval_id",
            sa.String(36),
            sa.ForeignKey("approval_requests.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("dispatch_state", sa.String(24), nullable=False),
        sa.Column("dispatch_error", sa.String(80), nullable=True),
    )
    op.create_index("ix_approval_decisions_task_id", "approval_decisions", ["task_id"])


def downgrade() -> None:
    raise RuntimeError(
        "Destructive downgrades are unsupported; apply a forward fix or restore backup"
    )
