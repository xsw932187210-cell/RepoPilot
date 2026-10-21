"""Publish only complete generations; preserve existing bound CH-03A approvals."""

import hashlib

import sqlalchemy as sa
from alembic import op

revision = "20261021_0005"
down_revision = "20261019_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "candidate_generations",
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("generation", sa.Integer(), primary_key=True),
        sa.Column("candidate_id", sa.String(64), sa.ForeignKey("candidates.id"), nullable=False),
        sa.Column("parent_candidate_id", sa.String(64), nullable=True),
        sa.Column("operation_id", sa.String(64), nullable=False),
        sa.UniqueConstraint("task_id", "operation_id", name="uq_generation_operation"),
    )
    op.create_table(
        "generation_heads",
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.String(64), sa.ForeignKey("candidates.id"), nullable=False),
    )
    # Only an existing explicit approval binding is eligible. Do not guess a head
    # from the latest test, a mutable workspace, or an unbound old checkpoint.
    metadata = sa.MetaData()
    requests = sa.Table("approval_requests", metadata, autoload_with=op.get_bind())
    candidates = sa.Table("candidates", metadata, autoload_with=op.get_bind())
    generations = sa.Table("candidate_generations", metadata, autoload_with=op.get_bind())
    heads = sa.Table("generation_heads", metadata, autoload_with=op.get_bind())
    rows = (
        op.get_bind()
        .execute(
            sa.select(
                requests.c.task_id,
                requests.c.candidate_id,
                requests.c.request_version,
                candidates.c.payload,
            )
            .join(candidates, candidates.c.id == requests.c.candidate_id)
            .order_by(requests.c.task_id, requests.c.request_version.desc())
        )
        .mappings()
    )
    seen = set()
    for row in rows:
        if row["task_id"] in seen:
            continue
        seen.add(row["task_id"])
        generation = row["payload"]["generation"]
        operation = hashlib.sha256(
            f"legacy-approval-v1:{row['task_id']}:{row['candidate_id']}".encode()
        ).hexdigest()
        op.get_bind().execute(
            generations.insert().values(
                task_id=row["task_id"],
                generation=generation,
                candidate_id=row["candidate_id"],
                parent_candidate_id=None,
                operation_id=operation,
            )
        )
        op.get_bind().execute(
            heads.insert().values(
                task_id=row["task_id"],
                generation=generation,
                candidate_id=row["candidate_id"],
            )
        )


def downgrade() -> None:
    raise RuntimeError(
        "Destructive downgrades are unsupported; restore backup or apply a forward fix"
    )
