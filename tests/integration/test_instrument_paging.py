"""Paging the catalog is stable when a reused ticker has several rows (#87, #100 review)."""

from __future__ import annotations

import random
import uuid

import pytest


def _add_reused_rows(dv_id: str, n: int) -> None:
    """``n`` holders of the ticker REUSE (plus one without an id), inserted shuffled."""
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Instrument

    rows = [f"QLR{k:03d}" for k in range(n)] + [None]
    random.Random(7).shuffle(rows)
    with SessionLocal() as db:
        for iid in rows:
            db.add(
                Instrument(
                    dataset_version_id=uuid.UUID(dv_id),
                    symbol="REUSE",
                    name="synthetic reused ticker",
                    asset_class="equity",
                    rows=1,
                    instrument_id=iid,
                )
            )
        db.commit()


@pytest.mark.parametrize("page", [1, 3, 7])
def test_pages_over_a_reused_ticker_have_no_duplicates_or_gaps(client, seeded, page):
    h, dv = seeded["headers"], seeded["dv"]
    _add_reused_rows(dv, 12)
    url = f"/api/v1/dataset-versions/{dv}/instruments"
    total = client.get(f"{url}?limit=1", headers=h).json()["total"]
    seen: list[tuple[str, str | None]] = []
    for offset in range(0, total, page):
        items = client.get(f"{url}?limit={page}&offset={offset}", headers=h).json()["items"]
        seen += [(i["symbol"], i["instrument_id"]) for i in items]
    assert len(seen) == total == len(set(seen))  # every row once, none twice
    reuse = [iid for sym, iid in seen if sym == "REUSE"]
    assert reuse == [*sorted(i for i in reuse if i), None]  # ids ascending, null last
    # The same request twice gives the same page.
    first = client.get(f"{url}?limit={page}&offset=10", headers=h).json()["items"]
    again = client.get(f"{url}?limit={page}&offset=10", headers=h).json()["items"]
    assert first == again
