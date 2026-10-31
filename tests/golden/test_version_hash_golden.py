"""A pinned dataset version hash: stored versions must keep their identity (golden)."""

from __future__ import annotations

import pandas as pd

from quantlab.data.schema import normalize_actions, normalize_bars
from quantlab.data.store import content_hash


def _acts(rows: list[dict]) -> pd.DataFrame:
    return normalize_actions(pd.DataFrame(rows))


# Computed by the content_hash of main at 40565cd, before optional action dates (#10) and
# symbol histories (#9) existed, for the fixed dataset below.
LEGACY_GOLDEN = "aa4392c84c51c990e327f6b7fd37197c5b0d4bf9f465d3ab179a87e040c78c4c"


def _golden_dataset() -> tuple[pd.DataFrame, pd.DataFrame]:
    bars = normalize_bars(
        pd.DataFrame(
            {
                "symbol": ["AAA", "AAA", "AAA", "BBB", "BBB"],
                "date": ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-02", "2024-01-03"],
                "open": [10.0, 10.5, 5.3, 20.0, 20.2],
                "high": [10.6, 10.8, 5.5, 20.4, 20.5],
                "low": [9.9, 10.2, 5.1, 19.8, 20.0],
                "close": [10.5, 10.6, 5.4, 20.1, 20.3],
                "volume": [1000.0, 1100.0, 2400.0, 500.0, 650.0],
            }
        )
    )
    actions = _acts(
        [
            {"symbol": "AAA", "ex_date": "2024-01-04", "kind": "split", "value": 2.0},
            {"symbol": "BBB", "ex_date": "2024-01-03", "kind": "dividend", "value": 0.1},
        ]
    )
    return bars, actions


def test_legacy_version_hash_is_unchanged_with_action_dates_and_identity(tmp_path):
    from quantlab.data.identity import legacy_history, normalize_history
    from quantlab.data.store import DatasetStore

    bars, actions = _golden_dataset()
    assert actions["declared_date"].isna().all()  # normalized: date columns present, NaT
    legacy = legacy_history(bars["symbol"])
    assert content_hash(bars, actions) == LEGACY_GOLDEN
    assert content_hash(bars, actions[["symbol", "ex_date", "kind", "value"]]) == LEGACY_GOLDEN
    assert content_hash(bars, actions, legacy) == LEGACY_GOLDEN
    store = DatasetStore(tmp_path)
    dv = store.write("golden", bars, actions, {"provider": "test"})
    assert dv.version == LEGACY_GOLDEN and store.verify(dv)
    # Real information still re-versions explicitly: a known date, or a real history.
    dated = actions.assign(declared_date=pd.Timestamp("2023-12-01"))
    assert content_hash(bars, dated) != LEGACY_GOLDEN
    history = normalize_history(legacy.assign(instrument_id=legacy["instrument_id"].radd("ID")))
    assert content_hash(bars, actions, history) != LEGACY_GOLDEN
