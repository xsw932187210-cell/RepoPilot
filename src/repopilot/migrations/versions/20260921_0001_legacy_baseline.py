"""Baseline the pre-CH-09 RepoPilot application schema."""

import sqlalchemy as sa
from alembic import op

revision = "20260921_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("repository_url", sa.String(length=500), nullable=False),
        sa.Column("issue_title", sa.String(length=240), nullable=False),
        sa.Column("issue_body", sa.Text(), nullable=False),
        sa.Column("base_branch", sa.String(length=120), nullable=False),
        sa.Column("test_command", sa.String(length=300), nullable=False),
        sa.Column("max_iterations", sa.Integer(), nullable=False),
        sa.Column("graph_thread_id", sa.String(length=80), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tasks_graph_thread_id", "tasks", ["graph_thread_id"], unique=True)
    op.create_index("ix_tasks_status", "tasks", ["status"], unique=False)
    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=60), nullable=False),
        sa.Column("node", sa.String(length=80), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"], unique=False)


def downgrade() -> None:
    raise RuntimeError(
        "Destructive schema downgrades are unsupported; restore a backup or apply a forward fix"
    )
