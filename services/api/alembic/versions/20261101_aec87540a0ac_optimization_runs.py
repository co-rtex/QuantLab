"""optimization_runs: portfolio optimizations computed by a worker job (#224)

Additive: a new table only, no change to existing tables or rows. Like ml_runs, a run is
removed with its workspace (CASCADE), and its dataset version is referenced as experiments
reference theirs (no ON DELETE: versions go only with their workspace). Downgrade drops
the table and with it only the stored optimization results.

Revision ID: aec87540a0ac
Revises: 18869f6a1dae
Create Date: 2026-11-01 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "aec87540a0ac"
down_revision: str | Sequence[str] | None = "18869f6a1dae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "optimization_runs"
JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_version_id", sa.Uuid(), nullable=False),
        sa.Column("params", JSON, nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "queued",
                "running",
                "completed",
                "failed",
                "cancelled",
                "timed_out",
                name="job_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("result", JSON, nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("code_version", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f(f"fk_{TABLE}_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version_id"],
            ["dataset_versions.id"],
            name=op.f(f"fk_{TABLE}_dataset_version_id_dataset_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f(f"fk_{TABLE}_job_id_jobs"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f(f"fk_{TABLE}_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{TABLE}")),
    )
    op.create_index(op.f(f"ix_{TABLE}_workspace_id"), TABLE, ["workspace_id"], unique=False)
    op.create_index(
        "ix_optimization_runs_version_created",
        TABLE,
        ["dataset_version_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_optimization_runs_version_created", table_name=TABLE)
    op.drop_index(op.f(f"ix_{TABLE}_workspace_id"), table_name=TABLE)
    op.drop_table(TABLE)
