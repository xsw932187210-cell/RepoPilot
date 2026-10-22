"""Confirmed stop requests and persisted execution/queue/approval timing."""

import sqlalchemy as sa
from alembic import op

revision = "20261022_0007"
down_revision = "20261022_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = [
        sa.Column("execution_timeout_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "deadline_includes_queue", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "deadline_includes_approval", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("execution_elapsed_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("queue_elapsed_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("approval_elapsed_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("phase_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_reason", sa.String(40), nullable=True),
        sa.Column("execution_resources", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("cleanup_pending", sa.JSON(), nullable=False, server_default="[]"),
    ]
    for column in columns:
        op.add_column("tasks", column)
    # Old tasks have no retrospective deadline or invented stop confirmation.
    op.execute(sa.text("UPDATE tasks SET phase_started_at = CURRENT_TIMESTAMP"))


def downgrade() -> None:
    raise RuntimeError("Restore backup or apply a forward fix; destructive downgrade unsupported")
