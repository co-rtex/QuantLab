"""instrument identity and delisting facts in the catalog (#9 part C)

Each catalog row (one per ticker in a dataset version) records the permanent instrument
it belonged to on its last bar and, when the version carries an instrument master, the
delisting date, reason and return. All columns are nullable: versions written before
identity existed have none, and the (dataset_version_id, symbol) key is unchanged.

Revision ID: 9a4c6e2f1b37
Revises: 7c3e5a1d9b20
Create Date: 2026-10-31 04:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9a4c6e2f1b37"
down_revision: str | Sequence[str] | None = "7c3e5a1d9b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "instruments"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("instrument_id", sa.String(length=32), nullable=True))
    op.add_column(TABLE, sa.Column("delisting_date", sa.Date(), nullable=True))
    op.add_column(TABLE, sa.Column("delisting_reason", sa.String(length=32), nullable=True))
    op.add_column(TABLE, sa.Column("delisting_return", sa.Float(), nullable=True))
    op.create_index(op.f(f"ix_{TABLE}_instrument_id"), TABLE, ["instrument_id"])


def downgrade() -> None:
    op.drop_index(op.f(f"ix_{TABLE}_instrument_id"), table_name=TABLE)
    op.drop_column(TABLE, "delisting_return")
    op.drop_column(TABLE, "delisting_reason")
    op.drop_column(TABLE, "delisting_date")
    op.drop_column(TABLE, "instrument_id")
