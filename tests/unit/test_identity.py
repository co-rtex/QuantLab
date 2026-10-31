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
    """OLD trades for 6 sessions, then the same security trades as NEW (rising prices)."""
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
    assert len(gone) and gone["delisting_reason"].isna().all()  # cause not modelled
    assert gone["delisting_return"].isna().all()  # unknown, never invented
    bars, acts = key_by_instrument(m.bars, m.actions, h)
    assert len(bars) == len(m.bars) and len(acts) == len(m.actions)
    assert bars["symbol"].str.startswith("QLSYN").all()


# ------------------------------------------------------------- review regressions
def test_back_to_back_intervals_canonicalize_and_hash_the_same():
    from quantlab.data.store import content_hash

    one = _hist([("I1", "X", "2010-01-01", None)])
    split = _hist([("I1", "X", "2010-01-01", "2015-01-01"), ("I1", "X", "2015-01-01", None)])
    pd.testing.assert_frame_equal(one, split)
    bars = make_bars({"X": [1.0, 2.0]})
    assert content_hash(bars, normalize_actions(None), one) == content_hash(
        bars, normalize_actions(None), split
    )
    # A gap between the intervals is real information and is kept.
    gap = _hist([("I1", "X", "2010-01-01", "2015-01-01"), ("I1", "X", "2015-02-02", None)])
    assert len(gap) == 2


def test_duplicate_history_rows_are_reported_as_duplicates():
    with pytest.raises(IdentityError, match="duplicate symbol history rows"):
        _hist([("I1", "X", "2010-01-01", None), ("I1", "X", "2010-01-01", None)])


def test_key_by_instrument_accepts_raw_vendor_timestamps():
    raw, acts, h = _rename_case()
    vendor = raw.assign(
        date=(raw["date"] + pd.Timedelta(hours=9, minutes=30)).dt.tz_localize("America/New_York")
    )
    bars, _ = key_by_instrument(vendor, acts, h)
    expected, _ = key_by_instrument(raw, acts, h)
    pd.testing.assert_frame_equal(bars, expected)
    tz_hist = h.assign(valid_from=h["valid_from"].dt.tz_localize("UTC") + pd.Timedelta(hours=14))
    pd.testing.assert_frame_equal(normalize_history(tz_hist), h)


def test_duplicate_bars_for_one_ticker_are_named_as_such():
    raw, acts, h = _rename_case()
    with pytest.raises(IdentityError, match="duplicate bars for one ticker"):
        key_by_instrument(pd.concat([raw, raw.iloc[[0]]]), acts, h)


def test_verify_returns_false_for_a_tampered_invalid_history(tmp_path):
    from quantlab.data.store import DatasetStore

    raw, acts, h = _rename_case()
    store = DatasetStore(tmp_path)
    dv = store.write("identity", raw, acts, {"provider": "test"}, symbol_history=h)
    bad = pd.concat([h, h.iloc[[0]].assign(instrument_id="Q9")])  # OLD -> two instruments
    bad.to_parquet(dv.path / "symbol_history.parquet", index=False)
    assert store.verify(dv) is False


# ---------------------------------------------------------- job helpers (#9 part C)
def _rename_and_reuse_history() -> pd.DataFrame:
    return _hist(
        [
            ("I1", "OLD", "2020-01-02", "2021-07-01"),
            ("I1", "NEW", "2021-07-01", None),  # renamed
            ("I2", "XYZ", "2020-01-02", "2021-03-01"),
            ("I3", "XYZ", "2021-07-01", None),  # ticker reused by another issuer
        ]
    )


def test_universe_resolves_as_of_the_backtest_start():
    from quantlab.data.identity import resolve_universe

    h = _rename_and_reuse_history()
    # Before the rename and the reuse: OLD names I1, XYZ names I2 (not the later I3).
    assert resolve_universe(h, ["old", "XYZ"], "2020-06-01") == (["I1", "I2"], [])
    # After both: the same tickers mean something else, or nothing.
    assert resolve_universe(h, ["OLD", "XYZ", "NEW"], "2022-01-03") == (["I1", "I3"], ["OLD"])
    # In the gap XYZ names nobody.
    assert resolve_universe(h, ["XYZ"], "2021-05-03") == ([], ["XYZ"])


def test_tickers_of_covers_every_ticker_an_instrument_had():
    from quantlab.data.identity import tickers_of

    h = _rename_and_reuse_history()
    assert tickers_of(h, ["I1"]) == ["NEW", "OLD"]
    assert tickers_of(h, ["I2"]) == ["XYZ"]  # loading XYZ also loads I3; keying separates them


def test_display_frame_shows_the_ticker_valid_on_each_date():
    from quantlab.data.identity import display_frame

    h = _rename_and_reuse_history()
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2021-01-04", "2021-08-02", "2021-04-01", "2021-08-02"]),
            "symbol": ["I1", "I1", "I2", "I3"],
            "quantity": [1.0, 2.0, 3.0, 4.0],
        }
    )
    out = display_frame(frame, h)
    assert out["symbol"].tolist() == ["OLD", "NEW", "XYZ", "XYZ"]
    assert out["instrument_id"].tolist() == ["I1", "I1", "I2", "I3"]
    assert list(out.columns) == ["date", "symbol", "instrument_id", "quantity"]
    # I2 traded as XYZ until 2021-03-01; a fill after that (its delisting) shows XYZ too.
    assert out["symbol"].iloc[2] == "XYZ"
    legacy = display_frame(frame.assign(symbol=["AAA"] * 4), legacy_history(["AAA"]))
    assert (legacy["symbol"] == legacy["instrument_id"]).all()


def test_label_ids_never_shows_a_later_ticker_when_avoidable():
    from quantlab.data.identity import label_ids

    h = _rename_and_reuse_history()
    # Before the rename I1 is OLD; I2 (retired 2021-03-01) shows its last ticker; I3 lists
    # later, so its first ticker is the only label it has.
    assert label_ids(h, ["I1", "I2", "I3"], "2021-05-03") == {"I1": "OLD", "I2": "XYZ", "I3": "XYZ"}
    assert label_ids(h, ["I1"], "2022-01-03") == {"I1": "NEW"}
    assert label_ids(h, ["UNKNOWN"], "2022-01-03") == {"UNKNOWN": "UNKNOWN"}


def test_universe_takes_a_later_listing_but_not_a_later_rename():
    from quantlab.data.identity import resolve_universe

    h = normalize_history(
        pd.concat(
            [
                _rename_and_reuse_history(),
                _hist([("I4", "FRESH", "2021-09-01", None)]),  # a genuine new listing
            ],
            ignore_index=True,
        )
    )
    # FRESH is I4's first ticker: it joins when it lists.
    assert resolve_universe(h, ["FRESH"], "2020-06-01", "2022-12-30") == (["I4"], [])
    # ...but not if the window ends before it lists.
    assert resolve_universe(h, ["FRESH"], "2020-06-01", "2021-01-04") == ([], ["FRESH"])
    # NEW is I1's *later* name: at a 2020 start I1 traded as OLD, so asking for NEW must
    # not select it by the name it will have (today's-ticker bias). It is unresolved.
    assert resolve_universe(h, ["NEW"], "2020-06-01", "2022-12-30") == ([], ["NEW"])
