"""Durable approved publication intents and remote receipts."""

import sqlalchemy as sa
from alembic import op

revision = "20261022_0006"
down_revision = "20261021_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publish_intents",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "decision_id",
            sa.String(36),
            sa.ForeignKey("approval_decisions.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("candidate_id", sa.String(64), sa.ForeignKey("candidates.id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("receipt", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_index("ix_publish_intents_task_id", "publish_intents", ["task_id"])


def downgrade() -> None:
    raise RuntimeError(
        "Destructive downgrades are unsupported; restore backup or apply a forward fix"
    )
