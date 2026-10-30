"""job foreign keys cascade so workspaces with results can be deleted

Deleting a workspace cascades to its jobs, but experiment_results, research_runs and
ml_runs referenced jobs without an ON DELETE rule, so deleting any workspace that had a
completed backtest failed with a foreign-key violation (reported as a 409 "retry").

Revision ID: 4f1c2a9e7b10
Revises: ba9da5bc3009
Create Date: 2026-10-30 21:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4f1c2a9e7b10"
down_revision: str | Sequence[str] | None = "ba9da5bc3009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, on delete) for every foreign key to jobs.id
FKS = [("experiment_results", "CASCADE"), ("research_runs", "SET NULL"), ("ml_runs", "SET NULL")]


def _recreate(table: str, ondelete: str | None) -> None:
    name = op.f(f"fk_{table}_job_id_jobs")
    op.drop_constraint(name, table, type_="foreignkey")
    op.create_foreign_key(name, table, "jobs", ["job_id"], ["id"], ondelete=ondelete)


def upgrade() -> None:
    """Upgrade schema."""
    for table, ondelete in FKS:
        _recreate(table, ondelete)


def downgrade() -> None:
    """Downgrade schema."""
    for table, _ in FKS:
        _recreate(table, None)
