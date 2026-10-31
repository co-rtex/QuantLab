"""Permanent instrument identity and point-in-time symbol resolution (issue #9)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from quantlab.backtest.engine import BacktestConfig, run_backtest
from quantlab.data.identity import (
    IdentityError,
    is_legacy,
    key_by_instrument,
    legacy_history,
    normalize_history,
    normalize_master,
    resolve,
    resolve_ids,
    symbols_as_of,
)
from quantlab.data.schema import normalize_actions
from quantlab.strategies.library import create_strategy
from tests.conftest import make_bars

FREE = dict(commission_bps=0, slippage_bps=0, fractional_shares=True)


def _hist(rows: list[tuple[str, str, str, str | None]]) -> pd.DataFrame:
    return normalize_history(
        pd.DataFrame(rows, columns=["instrument_id", "symbol", "valid_from", "valid_to"])
    )


# ------------------------------------------------------------- point-in-time resolution
@st.composite
def histories(draw):
    """Random valid histories with renames and ticker reuse on a small ticker alphabet."""
    tickers = ["AAA", "BBB", "CCC"]
    day0 = pd.Timestamp("2020-01-01")
    free_at = {t: 0 for t in tickers}  # first day each ticker is free again
    rows = []
    for k in range(draw(st.integers(1, 5))):
        start = draw(st.integers(0, 200))
        for _ in range(draw(st.integers(1, 3))):  # successive tickers of one instrument
            available = [t for t in tickers if free_at[t] < 10**6]
            if not available:
                break
            t = draw(st.sampled_from(available))
            start = max(start, free_at[t])
            length = draw(st.integers(1, 120))
            open_ended = draw(st.booleans()) and length > 100
            end = None if open_ended else start + length
            rows.append(
                (
                    f"I{k}",
                    t,
                    day0 + pd.Timedelta(days=start),
                    None if end is None else day0 + pd.Timedelta(days=end),
                )
            )
            free_at[t] = 10**6 if end is None else end
            if end is None:
                break
            start = end + draw(st.integers(0, 10))
    return _hist(rows)


def _reference(h: pd.DataFrame, symbol: str, d: pd.Timestamp) -> str | None:
    hit = h[
        (h["symbol"] == symbol)
        & (h["valid_from"] <= d)
        & (h["valid_to"].isna() | (d < h["valid_to"]))
    ]
    assert len(hit) <= 1
    return None if hit.empty else str(hit["instrument_id"].iloc[0])


@settings(max_examples=200, deadline=None)
@given(
    histories(),
    st.lists(
        st.tuples(st.sampled_from(["AAA", "BBB", "CCC", "ZZZ"]), st.integers(-5, 700)),
        min_size=1,
        max_size=30,
    ),
)
def test_as_of_resolution_never_uses_a_mapping_that_starts_later(h, queries):
    syms = pd.Series([q[0] for q in queries])
    dates = pd.Series([pd.Timestamp("2020-01-01") + pd.Timedelta(days=q[1]) for q in queries])
    got = resolve_ids(h, syms, dates)
    for s, d, g in zip(syms, dates, got, strict=True):
        assert g == _reference(h, s, d)
        if g is not None:
            used = h[(h["instrument_id"] == g) & (h["symbol"] == s)]
            assert (used["valid_from"] <= d).any()  # the interval had already started
            # Round trip: the instrument's ticker on that date is the queried ticker.
            assert symbols_as_of(h, pd.Series([g]), pd.Series([d])).iloc[0] == s


def test_rename_and_reuse_resolve_point_in_time():
    h = _hist(
        [
            ("I1", "FB", "2012-05-18", "2022-06-09"),
            ("I1", "META", "2022-06-09", None),
            ("I2", "META", "2005-01-03", "2010-01-04"),  # an unrelated earlier META
        ]
    )
    assert resolve(h, "FB", "2022-06-08") == "I1"
    assert resolve(h, "FB", "2022-06-09") is None  # FB retired on the rename date
    assert resolve(h, "META", "2022-06-08") is None  # not yet I1's ticker
    assert resolve(h, "META", "2022-06-09") == "I1"
    assert resolve(h, "META", "2008-06-02") == "I2"
    assert symbols_as_of(
        h,
        pd.Series(["I1", "I1"]),
        pd.Series([pd.Timestamp("2020-01-02"), pd.Timestamp("2023-01-03")]),
    ).tolist() == ["FB", "META"]


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [("I1", "XYZ", "2020-01-01", "2021-01-01"), ("I2", "XYZ", "2020-06-01", None)],
            "ticker maps to two instruments",
        ),
        (
            [("I1", "AAA", "2020-01-01", None), ("I1", "BBB", "2020-06-01", None)],
            "instrument has two tickers",
        ),
        ([("I1", "AAA", "2020-01-01", "2020-01-01")], "empty validity"),
        ([("I1", "a b", "2020-01-01", None)], "invalid symbols"),
    ],
)
def test_inconsistent_histories_are_rejected(rows, message):
    with pytest.raises(IdentityError, match=message):
        _hist(rows)


# ------------------------------------------------------------ keyed research frames
def _rename_case() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """OLD trades for 6 sessions, then the same company trades as NEW (flat price)."""
    prices = [50.0, 51.0, 52.0, 53.0, 54.0, 55.0, 56.0, 57.0, 58.0, 59.0, 60.0, 61.0]
    raw = make_bars({"X": prices, "B": [10.0] * 12})
    d = pd.DatetimeIndex(sorted(raw["date"].unique()))
    x = raw["symbol"] == "X"
    raw.loc[x & (raw["date"] < d[6]), "symbol"] = "OLD"
    raw.loc[x & (raw["date"] >= d[6]), "symbol"] = "NEW"
    h = _hist(
        [
            ("Q1", "OLD", str(d[0].date()), str(d[6].date())),
            ("Q1", "NEW", str(d[6].date()), None),
            ("Q2", "B", str(d[0].date()), None),
        ]
    )
    acts = normalize_actions(
        pd.DataFrame([{"symbol": "NEW", "ex_date": d[8], "kind": "dividend", "value": 0.5}])
    )
    return raw, acts, h


def test_backtest_across_a_rename_is_continuous():
    raw, acts, h = _rename_case()
    bars, keyed_acts = key_by_instrument(raw, acts, h)
    assert sorted(bars["symbol"].unique()) == ["Q1", "Q2"]
    assert keyed_acts["symbol"].tolist() == ["Q1"]
    cfg = BacktestConfig(universe=["Q1"], initial_capital=1000, delist_after_missing=3, **FREE)
    res = run_backtest(bars, keyed_acts, create_strategy("buy_and_hold"), cfg)
    assert len(res.trades) == 1  # one purchase; no phantom sale or re-buy at the rename
    assert res.trades.iloc[0]["side"] == "buy"
    shares = 1000 / 51.0
    assert res.equity["equity"].iloc[-1] == pytest.approx(shares * 61.0 + shares * 0.5)

    # Keyed by ticker, the same data shows the rename as a delisting.
    by_ticker = run_backtest(
        raw,
        acts,
        create_strategy("buy_and_hold"),
        cfg.model_copy(update={"universe": ["OLD", "NEW"]}),
    )
    assert "delisting" in set(by_ticker.trades["note"].dropna())


def test_reused_ticker_becomes_two_instruments():
    raw = make_bars({"XYZ": [10.0] * 4 + [None] * 3 + [80.0] * 4, "B": [5.0] * 11})
    d = pd.DatetimeIndex(sorted(raw["date"].unique()))
    h = _hist(
        [
            ("OLDCO", "XYZ", str(d[0].date()), str(d[4].date())),
            ("NEWCO", "XYZ", str(d[7].date()), None),
            ("BCO", "B", str(d[0].date()), None),
        ]
    )
    bars, _ = key_by_instrument(raw, normalize_actions(None), h)
    by_id = bars.groupby("symbol")["date"].agg(["min", "max", "count"])
    assert by_id.loc["OLDCO", "count"] == 4 and by_id.loc["NEWCO", "count"] == 4
    assert by_id.loc["OLDCO", "max"] < by_id.loc["NEWCO", "min"]
    # Keyed by ticker the series would "return" +700% across the gap.
    from quantlab.data.adjust import total_returns

    assert total_returns(bars, normalize_actions(None)).max().max() == pytest.approx(0.0)


def test_unmapped_or_doubly_listed_data_is_rejected():
    raw, acts, h = _rename_case()
    with pytest.raises(IdentityError, match="no instrument"):
        key_by_instrument(raw, acts, h[h["symbol"] != "NEW"])
    late = acts.assign(symbol="OLD")  # action reported under the retired ticker
    with pytest.raises(IdentityError, match="corporate actions have no instrument"):
        key_by_instrument(raw, late, h)
    d = pd.DatetimeIndex(sorted(raw["date"].unique()))
    both = pd.concat([raw, raw[(raw["symbol"] == "NEW")].assign(symbol="OLD")])
    h2 = _hist(
        [
            ("Q1", "OLD", str(d[0].date()), None),
            ("Q3", "NEW", str(d[0].date()), None),
            ("Q2", "B", str(d[0].date()), None),
        ]
    ).replace({"instrument_id": {"Q3": "Q1"}})
    with pytest.raises(IdentityError):
        key_by_instrument(both, acts, normalize_history(h2))


def test_legacy_history_is_the_identity_map():
    raw, acts, _ = _rename_case()
    h = legacy_history(raw["symbol"])
    assert is_legacy(h)
    bars, keyed = key_by_instrument(raw, acts, h)
    pd.testing.assert_frame_equal(bars, raw.sort_values(["symbol", "date"]).reset_index(drop=True))
    assert keyed["symbol"].tolist() == acts["symbol"].tolist()
    assert not is_legacy(_rename_case()[2])


# ---------------------------------------------------------------- instrument master
def test_master_validates_delisting_facts():
    ok = normalize_master(
        pd.DataFrame(
            [
                {
                    "instrument_id": "i1",
                    "name": "One",
                    "listing_date": "2010-01-04",
                    "delisting_date": "2020-03-02",
                    "delisting_reason": "bankruptcy",
                    "delisting_return": -0.9,
                },
                {"instrument_id": "I2", "name": "Two", "listing_date": "2015-01-02"},
            ]
        )
    )
    assert ok["instrument_id"].tolist() == ["I1", "I2"]
    assert np.isnan(ok.loc[1, "delisting_return"])
    bad = [
        {"instrument_id": "I1", "delisting_date": "2020-01-02", "delisting_reason": "vanished"},
        {"instrument_id": "I1", "delisting_date": "2020-01-02", "delisting_return": -1.5},
        {"instrument_id": "I1", "delisting_return": -0.2},  # return without a delisting
        {"instrument_id": "I1", "listing_date": "2021-01-04", "delisting_date": "2020-01-02"},
    ]
    for row in bad:
        with pytest.raises(IdentityError):
            normalize_master(pd.DataFrame([row]))


# ------------------------------------------------------------------------- storage
def test_store_persists_history_and_keeps_legacy_hashes(tmp_path):
    from quantlab.data.store import DatasetStore, content_hash

    raw, acts, h = _rename_case()
    store = DatasetStore(tmp_path)
    legacy = store.write("plain", raw, acts, {"provider": "test"})
    assert legacy.version == content_hash(raw, acts)  # unchanged algorithm for old data
    assert content_hash(raw, acts, legacy_history(raw["symbol"])) == legacy.version
    assert is_legacy(store.load_symbol_history(legacy))
    assert store.verify(legacy)

    dv = store.write("identity", raw, acts, {"provider": "test"}, symbol_history=h)
    assert dv.version != legacy.version  # the identity is part of the data
    assert dv.manifest["symbol_history"] is True
    pd.testing.assert_frame_equal(store.load_symbol_history(dv), h)
    assert store.verify(dv)

    with pytest.raises(IdentityError):  # data the history cannot key is never written
        store.write("broken", raw, acts, {"provider": "test"}, symbol_history=h.iloc[1:])
    assert not (tmp_path / "broken").exists() or not any((tmp_path / "broken").iterdir())


def test_synthetic_market_carries_permanent_ids():
    from quantlab.data.synthetic import SyntheticSpec, generate_market

    m = generate_market(SyntheticSpec(n_assets=8, seed=5, delisting_fraction=0.4))
    assert m.provenance["is_synthetic"] is True
    h, master = m.symbol_history, m.master
    assert not is_legacy(h)
    assert set(master["instrument_id"]) == set(h["instrument_id"])
    gone = master[master["delisting_date"].notna()]
    assert len(gone) and (gone["delisting_reason"] == "other").all()
    assert gone["delisting_return"].isna().all()  # unknown, never invented
    bars, acts = key_by_instrument(m.bars, m.actions, h)
    assert len(bars) == len(m.bars) and len(acts) == len(m.actions)
    assert bars["symbol"].str.startswith("QLSYN").all()
