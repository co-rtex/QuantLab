"""Corporate actions survive incremental ingestion intact (issue #50, finding 2).

Synthetic provider responses only.
"""

from __future__ import annotations

import pandas as pd
import pytest

from quantlab.data.providers import IngestionCheckpoint, ingest
from quantlab.data.schema import normalize_actions, normalize_bars
from tests.conftest import make_bars


class ScriptedProvider:
    """Serves fixed bars and actions; ``actions`` can be swapped between runs."""

    name = "scripted"
    is_synthetic = True

    def __init__(self, bars: pd.DataFrame, actions: pd.DataFrame) -> None:
        self.bars, self.actions = bars, actions

    def symbols(self) -> list[str]:
        return sorted(self.bars["symbol"].unique())

    def fetch_bars(self, symbol, start, end):
        b = self.bars
        return b[(b["symbol"] == symbol) & (b["date"] >= start) & (b["date"] <= end)]

    def fetch_actions(self, symbol, start, end):
        a = self.actions
        return a[(a["symbol"] == symbol) & (a["ex_date"] >= start) & (a["ex_date"] <= end)]

    def describe(self):
        return {"provider": "scripted", "is_synthetic": True}


def _acts(rows):
    return normalize_actions(pd.DataFrame(rows))


def test_regular_and_special_dividend_on_one_ex_date_are_both_kept():
    bars = make_bars({"A": [100.0] * 6})
    ex = bars["date"].iloc[3]
    acts = _acts(
        [
            {"symbol": "A", "ex_date": ex, "kind": "dividend", "value": 0.5},  # regular
            {"symbol": "A", "ex_date": ex, "kind": "dividend", "value": 2.0},  # special
        ]
    )
    res = ingest(ScriptedProvider(bars, acts), ["A"], bars["date"].min(), bars["date"].max())
    # main kept only the last row: 2.0, silently dropping the 0.5 regular dividend.
    assert sorted(res.actions["value"]) == [0.5, 2.0]
    assert res.actions["value"].sum() == pytest.approx(2.5)


def test_a_refetch_replaces_corrected_actions_instead_of_adding_them():
    bars = make_bars({"A": [100.0] * 6})
    ex = bars["date"].iloc[3]
    first = _acts([{"symbol": "A", "ex_date": ex, "kind": "dividend", "value": 0.4}])
    p = ScriptedProvider(bars, first)
    s, e = bars["date"].min(), bars["date"].max()
    done = ingest(p, ["A"], s, e)
    # The vendor corrects the amount; a backfill from scratch re-fetches the window.
    p.actions = _acts([{"symbol": "A", "ex_date": ex, "kind": "dividend", "value": 0.5}])
    again = ingest(p, ["A"], s, e, existing_bars=done.bars, existing_actions=done.actions)
    assert again.actions["value"].tolist() == [0.5]  # replaced, not 0.4 + 0.5


def test_existing_actions_outside_the_refetched_keys_are_kept():
    bars = make_bars({"A": [100.0] * 8, "B": [50.0] * 8})
    d = pd.DatetimeIndex(sorted(bars["date"].unique()))
    early = _acts([{"symbol": "A", "ex_date": d[1], "kind": "dividend", "value": 0.3}])
    p = ScriptedProvider(bars[bars["date"] <= d[3]], early)
    done = ingest(p, ["A", "B"], d[0], d[3])
    p.bars = bars
    p.actions = _acts([{"symbol": "B", "ex_date": d[6], "kind": "split", "value": 2.0}])
    resumed = ingest(
        p,
        ["A", "B"],
        d[0],
        d[-1],
        existing_bars=done.bars,
        existing_actions=done.actions,
        checkpoint=done.checkpoint,
    )
    got = resumed.actions.set_index(["symbol", "kind"])["value"].to_dict()
    assert got == {("A", "dividend"): 0.3, ("B", "split"): 2.0}
    assert isinstance(resumed.checkpoint, IngestionCheckpoint)
    assert len(resumed.bars) == len(normalize_bars(bars))
