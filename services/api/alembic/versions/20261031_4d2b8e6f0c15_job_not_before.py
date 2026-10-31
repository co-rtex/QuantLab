"""jobs.not_before: when a queued retry may next be claimed (#123)

Retry backoff was enforced only by the RQ scheduler's delay, so an early or forged queue
delivery ran a retry before its backoff expired. The database is the source of truth for
a job (ADR-0002): it now records when a queued job may next run, and claim() refuses it
before then. NULL means as soon as possible.

Revision ID: 4d2b8e6f0c15
Revises: b5e1d7c3a924
Create Date: 2026-10-31 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4d2b8e6f0c15"
down_revision: str | Sequence[str] | None = "b5e1d7c3a924"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A metadata-only change, but its brief ACCESS EXCLUSIVE lock must not queue behind a
    # long transaction and stall every job claim behind it: give up instead, and retry.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("jobs", sa.Column("not_before", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("jobs", "not_before")
