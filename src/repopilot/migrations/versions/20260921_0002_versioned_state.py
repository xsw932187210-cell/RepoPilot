"""Add task CAS state and owned-JSON schema versions."""

import sqlalchemy as sa
from alembic import op

revision = "20260921_0002"
down_revision = "20260921_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("state_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "tasks",
        sa.Column("result_schema_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "task_events",
        sa.Column("payload_schema_version", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Destructive schema downgrades are unsupported; restore a backup or apply a forward fix"
    )
