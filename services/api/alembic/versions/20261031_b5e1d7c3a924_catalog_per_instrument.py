"""catalog rows per (ticker, instrument) within a dataset version (#66)

With ticker reuse, one ticker belongs to two instruments in one version. The catalog
keyed by (version, symbol) kept only the later holder and lost the earlier one's delisting,
flag and sector. The key becomes (version, symbol, instrument_id) with NULLS NOT DISTINCT,
so versions without identity data (instrument_id NULL) still cannot repeat a ticker.

Revision ID: b5e1d7c3a924
Revises: 9a4c6e2f1b37
Create Date: 2026-10-31 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5e1d7c3a924"
down_revision: str | Sequence[str] | None = "9a4c6e2f1b37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "instruments"
OLD = "uq_instruments_dataset_version_id"
# Deliberately not the naming convention's uq_<table>_<first column>, which would reuse
# OLD's name for a different key (see models.Instrument).
NEW = "uq_instruments_version_symbol_instrument"


def upgrade() -> None:
    op.drop_constraint(op.f(OLD), TABLE, type_="unique")
    op.create_unique_constraint(
        op.f(NEW),
        TABLE,
        ["dataset_version_id", "symbol", "instrument_id"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    # The old key allows one row per (version, symbol). Refuse rather than silently
    # deleting the rows a reused ticker produced.
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                f"SELECT count(*) FROM (SELECT 1 FROM {TABLE} "  # noqa: S608 - constant name
                "GROUP BY dataset_version_id, symbol HAVING count(*) > 1) d"
            )
        )
        .scalar_one()
    )
    if duplicates:
        raise RuntimeError(
            f"cannot downgrade: {duplicates} (dataset_version_id, symbol) pairs have rows "
            "for more than one instrument (ticker reuse); delete those dataset versions first"
        )
    op.drop_constraint(op.f(NEW), TABLE, type_="unique")
    op.create_unique_constraint(op.f(OLD), TABLE, ["dataset_version_id", "symbol"])
