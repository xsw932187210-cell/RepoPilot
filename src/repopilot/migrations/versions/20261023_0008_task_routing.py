"""Persist explicit execution strategy; legacy tasks retain workflow behavior."""

import sqlalchemy as sa
from alembic import op

revision = "20261023_0008"
down_revision = "20261022_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column(
        "execution_strategy", sa.String(16), nullable=False, server_default="workflow",
    ))


def downgrade() -> None:
    raise RuntimeError("Restore backup or apply a forward fix; destructive downgrade unsupported")
