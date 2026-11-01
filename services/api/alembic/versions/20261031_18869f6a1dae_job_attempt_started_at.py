"""jobs.attempt_started_at: when the current attempt was claimed (#167)

The reaper timed a job out by ``started_at``, which is attempt 1's start and is kept across
retries, so time spent waiting for a worker after a lost one counted toward
``timeout_seconds``: a job requeued after a long outage was timed out at once. The worker's
own check and RQ's job timeout were already per attempt. ``started_at`` keeps meaning
"first started"; this column is set on every claim, and the reaper times the current
attempt by it. NULL (rows from before this revision): the reaper falls back to
``started_at``, as before.

Revision ID: 18869f6a1dae
Revises: 4d2b8e6f0c15
Create Date: 2026-10-31 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "18869f6a1dae"
down_revision: str | Sequence[str] | None = "4d2b8e6f0c15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Metadata only, as in 4d2b8e6f0c15: its brief ACCESS EXCLUSIVE lock must not queue
    # behind a long transaction and stall every job claim; give up instead, and retry.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "jobs", sa.Column("attempt_started_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("jobs", "attempt_started_at")
