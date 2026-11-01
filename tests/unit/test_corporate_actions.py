"""Corporate-action placement, ordering and ingestion gates (issue #10).

Every expected value here is derived by hand from the share-count bookkeeping of a holder,
independently of ``quantlab.data.adjust``: a split of ``s`` multiplies the shares held and
divides the price; a cash dividend of ``d`` per share held at that moment is paid in cash
and lowers the price by ``d``. On a "flat" security (one whose value per original share
never changes) a buy-and-hold account must therefore keep its equity exactly, and every
total return must be zero.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from quantlab.backtest.engine import BacktestConfig, run_backtest
from quantlab.data.adjust import adjusted_close, place_actions, total_returns
from quantlab.data.calendar import trading_days
from quantlab.data.quality import QualityGateError, assess_quality, enforce_gates
from quantlab.data.schema import SchemaError, normalize_actions, normalize_bars
from quantlab.data.store import content_hash
from quantlab.strategies.library import create_strategy
from tests.conftest import make_bars

FREE = dict(commission_bps=0, slippage_bps=0, fractional_shares=True)


def _acts(rows: list[dict]) -> pd.DataFrame:
    return normalize_actions(pd.DataFrame(rows))


def _sessions(bars: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(sorted(bars["date"].unique()))


# --------------------------------------------------------------- defect 1: merged ordering
@pytest.mark.parametrize(
    ("first", "second", "after"),
    [
        # Split on Saturday, then a dividend on Sunday quoted per *post-split* share:
        # 10 shares @ 100 -> 20 shares @ 50 -> 20 * $1 cash, price 49.
        (("split", 2.0), ("dividend", 1.0), 49.0),
        # Dividend on Saturday per pre-split share, then the split on Sunday:
        # 10 shares @ 100 -> $10 cash, price 99 -> 20 shares @ 49.5.
        (("dividend", 1.0), ("split", 2.0), 49.5),
    ],
)
def test_actions_merged_onto_one_session_apply_in_ex_date_order(first, second, after):
    bars = make_bars({"A": [100.0] * 4 + [after] * 3})  # Jan 2-5 (Tue-Fri), Jan 8-10
    acts = _acts(
        [
            {"symbol": "A", "ex_date": "2024-01-06", "kind": first[0], "value": first[1]},
            {"symbol": "A", "ex_date": "2024-01-07", "kind": second[0], "value": second[1]},
        ]
    )
    res = run_backtest(
        bars,
        acts,
        create_strategy("buy_and_hold"),
        BacktestConfig(universe=["A"], initial_capital=1000, **FREE),
    )
    eq = res.equity
    assert eq["equity"].iloc[-1] == pytest.approx(1000, abs=1e-9)
    assert eq["cum_dividends"].iloc[-1] == pytest.approx(20 if first[0] == "split" else 10)
    res.ledger.check_invariants()
    tr = total_returns(bars, acts)["A"].dropna()
    np.testing.assert_allclose(tr.to_numpy(), 0.0, atol=1e-12)
    adj = adjusted_close(bars, acts)["A"]
    np.testing.assert_allclose(adj.to_numpy(), after, rtol=1e-12)


def test_same_ex_date_dividend_is_per_pre_split_share():
    # Documented convention: on one ex-date the dividend is paid before the split.
    bars = make_bars({"A": [100.0, 100.0, 49.0]})
    d = bars["date"].unique()
    acts = _acts(
        [
            {"symbol": "A", "ex_date": d[2], "kind": "split", "value": 2.0},
            {"symbol": "A", "ex_date": d[2], "kind": "dividend", "value": 2.0},
        ]
    )
    tr = total_returns(bars, acts)["A"].to_numpy()
    assert tr[2] == pytest.approx((2 * 49.0 + 2.0) / 100.0 - 1)


# ------------------------------------------------- defect 2: returns vs. adjusted closes
def test_total_returns_keep_an_action_that_lands_on_a_missing_bar():
    # A prints no bar on the dividend's ex-date (B trades, so the session exists).
    bars = make_bars({"A": [100.0, 100.0, None, 98.0, 98.0], "B": [10.0] * 5})
    d = _sessions(bars)
    acts = _acts([{"symbol": "A", "ex_date": d[2], "kind": "dividend", "value": 2.0}])
    tr = total_returns(bars, acts)["A"]
    assert np.isnan(tr.iloc[2])  # no bar, no return
    assert tr.iloc[3] == pytest.approx((98.0 + 2.0) / 100.0 - 1)  # dividend kept
    adj = adjusted_close(bars, acts)["A"]
    # The two views agree across the gap.
    assert adj.iloc[3] / adj.iloc[1] - 1 == pytest.approx(tr.iloc[3], abs=1e-12)


@pytest.mark.parametrize("execution", ["next_open", "next_close"])
def test_strategy_returns_view_matches_adjusted_closes_across_a_gap(execution):
    from quantlab.strategies.base import MarketView, Strategy, StrategyParams

    bars = make_bars({"A": [100.0] * 3 + [None] + [49.0] * 3, "B": [10.0] * 7})
    d = _sessions(bars)
    acts = _acts(
        [
            {"symbol": "A", "ex_date": d[3], "kind": "split", "value": 2.0},
            {"symbol": "A", "ex_date": d[3], "kind": "dividend", "value": 2.0},
        ]
    )
    views: list[MarketView] = []

    class Spy(Strategy):
        key, name, description, Params = "spy", "Spy", "", StrategyParams

        def target_weights(self, view: MarketView) -> pd.Series | None:
            views.append(view)
            return pd.Series({"A": 1.0}) if len(views) == 1 else None

    cfg = BacktestConfig(
        universe=["A", "B"], rebalance="daily", execution=execution, initial_capital=1000, **FREE
    )
    res = run_backtest(bars, acts, Spy(), cfg)
    # Never overstated on the gap session (main credited the dividend there while still
    # marking the position at the cum-dividend close).
    np.testing.assert_allclose(res.equity["equity"].to_numpy(), 1000.0, rtol=1e-12)
    last = views[-1]
    a_ret = last.returns["A"].dropna()
    a_close = last.close["A"].dropna()
    np.testing.assert_allclose((1 + a_ret).prod(), a_close.iloc[-1] / a_close.iloc[0], rtol=1e-12)
    np.testing.assert_allclose(a_ret.to_numpy(), 0.0, atol=1e-12)


# ------------------------------------------------------- defect 3: engine bookkeeping
def test_engine_meta_separates_late_and_ignored_actions():
    bars = make_bars({"A": [100.0, 100.0, None, 50.0, 50.0], "B": [10.0] * 5})
    d = _sessions(bars)
    acts = _acts(
        [
            {"symbol": "A", "ex_date": d[1], "kind": "dividend", "value": 0.0001},
            {"symbol": "A", "ex_date": d[2], "kind": "split", "value": 2.0},  # gap: next bar
            {"symbol": "A", "ex_date": "2024-01-06", "kind": "dividend", "value": 0.0001},
            {"symbol": "A", "ex_date": d[0] - pd.Timedelta(days=40), "kind": "split", "value": 3},
        ]
    )
    res = run_backtest(
        bars, acts, create_strategy("buy_and_hold"), BacktestConfig(universe=["A"], **FREE)
    )
    assert "unmatched_corporate_actions" not in res.meta
    assert res.meta["corporate_actions"] == {
        "on_ex_date": 1,
        "applied_next_session": 2,
        "outside_history": 1,
    }


def test_place_actions_reports_rows_and_placement():
    bars = make_bars({"A": [1.0, 1.0, None, 1.0], "B": [1.0] * 4})
    close = bars.pivot(index="date", columns="symbol", values="close")
    d = close.index
    acts = _acts(
        [
            {"symbol": "A", "ex_date": d[1], "kind": "split", "value": 2.0},
            {"symbol": "A", "ex_date": d[2], "kind": "split", "value": 2.0},
            {"symbol": "A", "ex_date": d[3] + pd.Timedelta(days=9), "kind": "split", "value": 2},
            {"symbol": "ZZZ", "ex_date": d[1], "kind": "split", "value": 2.0},
        ]
    )
    placed = place_actions(close, acts).set_index(["symbol", "ex_date"])
    assert placed.loc[("A", d[1]), "row"] == 1
    assert placed.loc[("A", d[1]), "placement"] == "ex_date"
    assert placed.loc[("A", d[2]), "row"] == 3
    assert placed.loc[("A", d[2]), "placement"] == "next_bar"
    assert placed.loc[("A", d[3] + pd.Timedelta(days=9)), "placement"] == "after_history"
    assert placed.loc[("ZZZ", d[1]), "placement"] == "no_bars"
    assert (placed.loc[placed["placement"].isin(["after_history", "no_bars"]), "row"] == -1).all()


# ------------------------------------------------------------- property: conservation
@st.composite
def flat_security(draw):
    """A flat-value security with random splits/dividends on random (non-)session dates.

    Returns (bars, actions, n_gaps). B trades every session; A may skip sessions.
    """
    sessions = trading_days("2024-01-02", "2024-03-29")
    n = draw(st.integers(12, 40))
    sessions = sessions[:n]
    calendar_days = pd.date_range(sessions[0] + pd.Timedelta(days=1), sessions[-1])
    events = draw(
        st.lists(
            st.tuples(
                st.integers(0, len(calendar_days) - 1),
                st.sampled_from(["split", "dividend"]),
                st.sampled_from([0.1, 0.25, 0.5, 1.5, 2.0, 3.0, 4.0, 10.0]),
                st.floats(0.001, 0.2),
            ),
            max_size=8,
        )
    )
    gaps = draw(st.sets(st.integers(1, n - 2), max_size=n // 3))
    # Same-day convention: dividends before splits; otherwise by date.
    rows = sorted(
        ((calendar_days[i], kind, ratio, frac) for i, kind, ratio, frac in events),
        key=lambda e: (e[0], e[1] != "dividend"),
    )
    price = 100.0
    closes: list[float] = []
    action_rows: list[dict] = []
    it = iter(rows)
    pending = next(it, None)
    for s in sessions:
        while pending is not None and pending[0] <= s:
            day, kind, ratio, frac = pending
            if kind == "split":
                price /= ratio
                action_rows.append({"symbol": "A", "ex_date": day, "kind": "split", "value": ratio})
            else:
                div = price * frac
                price -= div
                action_rows.append(
                    {"symbol": "A", "ex_date": day, "kind": "dividend", "value": div}
                )
            pending = next(it, None)
        closes.append(price)
    a = [None if k in gaps else c for k, c in enumerate(closes)]
    bars = make_bars({"A": a, "B": [10.0] * n})
    assert list(_sessions(bars)) == list(sessions)
    return bars, normalize_actions(pd.DataFrame(action_rows) if action_rows else None)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(flat_security())
def test_flat_value_is_conserved_by_ledger_and_adjusted_view(case):
    bars, acts = case
    res = run_backtest(
        bars,
        acts,
        create_strategy("buy_and_hold"),
        BacktestConfig(universe=["A"], initial_capital=1000, delist_after_missing=63, **FREE),
    )
    res.ledger.check_invariants()
    np.testing.assert_allclose(res.equity["equity"].to_numpy(), 1000.0, rtol=1e-9)
    tr = total_returns(bars, acts)["A"].dropna()
    np.testing.assert_allclose(tr.to_numpy(), 0.0, atol=1e-9)
    adj = adjusted_close(bars, acts)["A"].dropna()
    np.testing.assert_allclose(adj.to_numpy(), adj.iloc[-1], rtol=1e-9)
    # As-of views agree on returns with the full view (levels differ by a constant).
    for as_of in _sessions(bars)[::5]:
        part = adjusted_close(bars, acts, as_of=as_of)["A"].dropna()
        np.testing.assert_allclose(part.to_numpy(), part.iloc[-1], rtol=1e-9)


# ----------------------------------------------------------------------- ingestion gates
def _base() -> tuple[pd.DataFrame, pd.DataFrame]:
    # A splits 2-for-1 at d[5]; both symbols drift by about 1% a day.
    a = [100.0, 101.0, 102.0, 101.0, 100.0, 50.5, 51.0, 50.0, 50.5, 51.0]
    b = [10.0, 10.1, 10.2, 10.1, 10.0, 10.1, 10.2, 10.1, 10.0, 10.1]
    bars = make_bars({"A": a, "B": b})
    d = _sessions(bars)
    return bars, _acts([{"symbol": "A", "ex_date": d[5], "kind": "split", "value": 2.0}])


def _failing(bars: pd.DataFrame, acts: pd.DataFrame) -> set[str]:
    report = assess_quality(bars, acts)
    return {c.name for c in report.checks if c.status == "fail"}


def test_clean_dataset_passes_every_gate():
    bars, acts = _base()
    report = assess_quality(bars, acts)
    assert report.status == "pass", [c for c in report.checks if c.status != "pass"]
    enforce_gates(report)  # does not raise


def _gate_cases() -> dict[str, tuple[pd.DataFrame, pd.DataFrame, str]]:
    bars, acts = _base()
    d = _sessions(bars)
    split = {"symbol": "A", "ex_date": d[5], "kind": "split", "value": 2.0}
    return {
        # A split recorded one session late: its bar shows a 2x jump the ratio cannot
        # explain (the true ex-date shows a -50% move, a warning on its own).
        "late_split": (
            bars,
            _acts([{**split, "ex_date": d[6]}]),
            "split_price_mismatch",
        ),
        # 4-for-1 recorded for a 2-for-1 split: the split bar doubles in value.
        "wrong_ratio": (bars, _acts([{**split, "value": 4.0}]), "split_price_mismatch"),
        # 25.0 instead of 0.25: the price does not drop, so the data fabricates a +25% day.
        "dividend_in_wrong_unit": (
            bars,
            _acts([split, {**split, "ex_date": d[2], "kind": "dividend", "value": 25.0}]),
            "dividend_price_mismatch",
        ),
        "phantom_split": (
            bars,
            _acts([split, {**split, "ex_date": d[2]}]),
            "split_price_mismatch",
        ),
        "duplicate_action": (bars, _acts([split, split]), "duplicate_corporate_actions"),
        "noop_split": (
            bars,
            _acts([split, {**split, "ex_date": d[2], "value": 1.0}]),
            "split_ratio_invalid",
        ),
        "absurd_split": (
            bars,
            _acts([split, {**split, "ex_date": d[2], "value": 1e6}]),
            "split_ratio_invalid",
        ),
        "dividend_exceeds_price": (
            bars,
            _acts([split, {**split, "ex_date": d[2], "kind": "dividend", "value": 150.0}]),
            "dividend_exceeds_price",
        ),
        # 1-for-10 recorded where the prices show 1-for-40: the reverse-split bar loses
        # three quarters of its value, beyond the 1/3x reverse-split limit.
        "reverse_ratio_wrong": (
            make_bars({"R": [0.5, 0.52, 0.5, 5.0, 5.1]}),
            _acts(
                [
                    {
                        "symbol": "R",
                        "ex_date": make_bars({"R": [1.0] * 4})["date"].iloc[3],
                        "kind": "split",
                        "value": 0.025,
                    }
                ]
            ),
            "split_price_mismatch",
        ),
        "duplicate_bar": (
            normalize_bars(pd.concat([bars, bars.iloc[[3]]])),
            acts,
            "duplicate_bars",
        ),
    }


@pytest.mark.parametrize("case", sorted(_gate_cases()))
def test_each_malformed_input_class_blocks_ingestion(case):
    bars, acts, check = _gate_cases()[case]
    report = assess_quality(bars, acts)
    assert check in _failing(bars, acts), [(c.name, c.status) for c in report.checks]
    with pytest.raises(QualityGateError) as exc:
        enforce_gates(report)
    msg = str(exc.value)
    assert check in msg  # readable: names the check, its count and a sample row
    assert "e.g. {" in msg and "symbol" in msg  # with a sample row


def test_genuine_large_move_is_a_warning_not_a_gate():
    # +80% (e.g. a takeover bid) is not a split signature and must not block ingestion.
    bars = make_bars({"A": [100.0] * 4 + [180.0] * 4})
    report = assess_quality(bars, _acts([]))
    by = {c.name: c for c in report.checks}
    assert by["extreme_returns"].status == "warn"
    assert by["split_like_returns"].status == "pass"
    enforce_gates(report)


def _warn_cases() -> dict[str, tuple[pd.DataFrame, pd.DataFrame, str]]:
    """Legitimate real-market patterns that must be reported but never block ingestion."""
    bars, _ = _base()
    d = _sessions(bars)
    split = {"symbol": "A", "ex_date": d[5], "kind": "split", "value": 2.0}
    return {
        # Prices alone cannot tell an unrecorded split from a genuine move of that size.
        "missing_split": (bars, _acts([]), "split_like_returns"),
        "penny_stock_tick": (
            make_bars({"P": [0.02, 0.02, 0.01, 0.01]}),
            _acts([]),
            "split_like_returns",
        ),
        "biotech_readout": (
            make_bars({"X": [40.0, 40.4, 20.2, 20.0]}),
            _acts([]),
            "split_like_returns",
        ),
        "takeover_bid": (
            make_bars({"T": [10.0, 10.1, 20.4, 20.5]}),
            _acts([]),
            "split_like_returns",
        ),
        # 1-for-10 reverse split of a distressed name that also falls 30% that day.
        "reverse_split_selloff": (
            make_bars({"R": [0.5, 0.52, 0.5, 3.5, 3.4]}),
            _acts(
                [
                    {
                        "symbol": "R",
                        "ex_date": make_bars({"R": [1.0] * 4})["date"].iloc[3],
                        "kind": "split",
                        "value": 0.1,
                    }
                ]
            ),
            "split_bar_large_move",
        ),
        # 3-for-1 recorded for a 2-for-1 split: +51%, worth a look but not certain.
        "ratio_off_by_half": (bars, _acts([{**split, "value": 3.0}]), "split_bar_large_move"),
        # Actions the engine never applies, and provenance-only dates, only warn.
        "orphan_symbol": (
            bars,
            _acts([split, {**split, "symbol": "ZZZ"}]),
            "orphan_corporate_actions",
        ),
        "before_first_bar": (
            bars,
            _acts(
                [
                    split,
                    {
                        **split,
                        "ex_date": d[0] - pd.Timedelta(days=30),
                        "kind": "dividend",
                        "value": 1.0,
                    },
                ]
            ),
            "corporate_actions_before_history",
        ),
        "declared_after_ex": (
            bars,
            _acts([{**split, "declared_date": d[7]}]),
            "corporate_action_date_order",
        ),
        "paid_before_record": (
            bars,
            _acts([{**split, "record_date": d[6], "pay_date": d[5]}]),
            "corporate_action_date_order",
        ),
        # An announced action, or a terminal distribution after the last trade.
        "after_last_bar": (
            bars,
            _acts(
                [
                    split,
                    {
                        **split,
                        "ex_date": d[-1] + pd.Timedelta(days=30),
                        "kind": "dividend",
                        "value": 1.0,
                    },
                ]
            ),
            "corporate_actions_after_history",
        ),
    }


@pytest.mark.parametrize("case", sorted(_warn_cases()))
def test_legitimate_patterns_warn_but_do_not_block(case):
    bars, acts, check = _warn_cases()[case]
    report = assess_quality(bars, acts)
    by = {c.name: c for c in report.checks}
    assert by[check].status == "warn", [(c.name, c.status) for c in report.checks]
    enforce_gates(report)


def test_several_dividends_on_one_ex_date_warn_without_blocking():
    # #210. A regular and a special dividend on one ex-date are legitimate, but so would be
    # a source that appends a correction (0.25, then 0.26) as a second row: both are paid.
    # Different amounts on one ex-date are therefore reported, not blocked.
    bars = make_bars({"B": [10.0, 10.1, 10.2, 10.1, 10.0, 10.1, 10.2, 10.1]})
    d = _sessions(bars)
    div = {"symbol": "B", "ex_date": d[4], "kind": "dividend"}
    report = assess_quality(bars, _acts([{**div, "value": 0.25}, {**div, "value": 0.26}]))
    by = {c.name: c for c in report.checks}
    check = by["multiple_dividends_on_ex_date"]
    assert (check.status, check.count) == ("warn", 2)
    assert sorted(r["value"] for r in check.sample) == [0.25, 0.26]
    assert by["duplicate_corporate_actions"].status == "pass"
    enforce_gates(report)
    # One dividend is not reported, and an identical repeat still blocks.
    one = {c.name: c for c in assess_quality(bars, _acts([{**div, "value": 0.25}])).checks}
    assert one["multiple_dividends_on_ex_date"].status == "pass"
    assert "duplicate_corporate_actions" in _failing(bars, _acts([{**div, "value": 0.25}] * 2))


def test_dividend_on_a_symbols_first_bar_passes():
    # A download window that starts on an ex-date has no previous close to compare with.
    bars = make_bars({"Y": [50.0, 50.5, 50.2, 50.4]})
    acts = _acts(
        [{"symbol": "Y", "ex_date": bars["date"].iloc[0], "kind": "dividend", "value": 0.25}]
    )
    assert assess_quality(bars, acts).status == "pass"


def test_large_special_dividend_with_matching_price_drop_passes():
    # $30 special dividend on a $100 stock that opens about $30 lower.
    bars = make_bars({"S": [100.0, 101.0, 71.5, 72.0]})
    acts = _acts(
        [{"symbol": "S", "ex_date": bars["date"].iloc[2], "kind": "dividend", "value": 30.0}]
    )
    assert assess_quality(bars, acts).status == "pass"


def test_quality_accepts_legacy_four_column_actions():
    bars, acts = _base()
    report = assess_quality(bars, acts[["symbol", "ex_date", "kind", "value"]])
    assert report.status == "pass"


def test_ex_date_without_a_bar_warns_but_does_not_block():
    # Ex-date on Saturday 2024-01-06; the split applies at Monday's bar.
    bars = make_bars({"A": [100.0] * 4 + [50.0] * 6, "B": [10.0] * 10})
    acts = _acts([{"symbol": "A", "ex_date": "2024-01-06", "kind": "split", "value": 2.0}])
    report = assess_quality(bars, acts)
    by = {c.name: c for c in report.checks}
    assert by["corporate_action_ex_date_without_bar"].status == "warn"
    assert by["corporate_action_ex_date_without_bar"].count == 1
    enforce_gates(report)


def test_large_distribution_may_pay_before_its_ex_date():
    # FINRA Rule 11140(b): for distributions of 25% or more the ex-date is the first
    # business day after the payable date, so pay/record before ex is legitimate.
    bars, acts = _base()
    d = _sessions(bars)
    acts = _acts(
        [
            {
                "symbol": "A",
                "ex_date": d[5],
                "kind": "split",
                "value": 2.0,
                "declared_date": d[0],
                "record_date": d[3],
                "pay_date": d[4],
            }
        ]
    )
    assert assess_quality(bars, acts).status == "pass"


# ------------------------------------------------------------------ schema & versioning
def test_optional_action_dates_round_trip_and_keep_old_hashes_stable():
    bars, acts = _base()
    legacy = acts[["symbol", "ex_date", "kind", "value"]]
    assert content_hash(bars, acts) == content_hash(bars, legacy)
    dated = _acts(
        [
            {
                "symbol": "A",
                "ex_date": acts["ex_date"].iloc[0],
                "kind": "split",
                "value": 2.0,
                "declared_date": "2024-01-02",
            }
        ]
    )
    assert dated["declared_date"].iloc[0] == pd.Timestamp("2024-01-02")
    assert pd.isna(dated["pay_date"].iloc[0])
    assert content_hash(bars, dated) != content_hash(bars, acts)


def test_unparseable_action_dates_are_rejected():
    with pytest.raises(SchemaError):
        _acts(
            [
                {
                    "symbol": "A",
                    "ex_date": "2024-01-02",
                    "kind": "split",
                    "value": 2.0,
                    "pay_date": "not a date",
                }
            ]
        )


# ---------------------------------------------- lead review of #27: false positives
def _blocking(bars: pd.DataFrame, acts: pd.DataFrame) -> list[str]:
    return [c.name for c in assess_quality(bars, acts).blocking]


def test_full_history_actions_file_with_windowed_bars_is_accepted():
    # A yfinance-style actions file going back years, with bars only for 2024.
    bars = make_bars({"A": [100.0, 101.0, 102.0, 101.5]}, start="2024-01-02")
    acts = _acts([{"symbol": "A", "ex_date": "2020-08-31", "kind": "split", "value": 4.0}])
    report = assess_quality(bars, acts)
    assert {c.name: c.status for c in report.checks}["corporate_actions_before_history"] == "warn"
    enforce_gates(report)


def test_actions_file_covering_extra_symbols_is_accepted():
    bars = make_bars({"A": [100.0, 101.0, 102.0]})
    acts = _acts([{"symbol": "OTHER", "ex_date": "2024-01-03", "kind": "dividend", "value": 1}])
    assert _blocking(bars, acts) == []


@pytest.mark.parametrize(
    ("price", "excess", "blocks"),
    [
        (10.0, 1e-7, False),  # float noise
        (10.0, 0.004, False),  # rounding within half a cent
        (10.0, 0.02, True),  # two cents above the high: inconsistent
        (0.5, 0.00004, False),  # sub-dollar: within half of the $0.0001 tick
        (0.5, 0.001, True),
    ],
)
def test_ohlc_consistency_uses_a_half_tick_tolerance(price, excess, blocks):
    bars = make_bars({"A": [price] * 3})
    bars.loc[1, "close"] = bars.loc[1, "high"] + excess
    assert ("ohlc_inconsistent" in _blocking(bars, _acts([]))) is blocks


@pytest.mark.parametrize(
    ("closes", "dividend", "check"),
    [
        # 12% special dividend on a +7% day: ex price 88 x 1.07 = 94.16.
        ([100.0, 100.0, 94.16, 94.0], 12.0, "dividend_not_reflected"),
        # 15% dividend across a gap that also gained 10%: 85 x 1.10 = 93.5.
        ([100.0, 100.0, None, 93.5, 93.0], 15.0, "dividend_not_reflected"),
        # Cents entered as dollars in the 2-10% band: $5 on $100 with no price drop.
        ([100.0, 100.0, 100.5, 100.0], 5.0, "dividend_not_reflected"),
    ],
)
def test_plausible_dividend_mismatches_warn_without_blocking(closes, dividend, check):
    bars = make_bars({"A": closes, "B": [10.0 + 0.1 * k for k in range(len(closes))]})
    d = _sessions(bars)
    ex = d[3] if None in closes else d[2]
    acts = _acts([{"symbol": "A", "ex_date": ex, "kind": "dividend", "value": dividend}])
    report = assess_quality(bars, acts)
    assert {c.name: c.status for c in report.checks}[check] == "warn"
    assert report.blocking == []


def test_reverse_split_on_a_distress_selloff_is_accepted():
    # 1-for-10 on a -50% day: raw 0.50 -> 2.50, total return 0.1 x 2.5 / 0.5 - 1 = -50%.
    bars = make_bars({"R": [0.5, 0.52, 0.5, 2.5, 2.45]})
    d = _sessions(bars)
    acts = _acts([{"symbol": "R", "ex_date": d[3], "kind": "split", "value": 0.1}])
    report = assess_quality(bars, acts)
    assert {c.name: c.status for c in report.checks}["split_bar_large_move"] == "warn"
    assert report.blocking == []
    # The same move on a *forward* split (2x down) still blocks: a ratio error.
    fwd = make_bars({"F": [100.0, 101.0, 100.0, 25.0, 25.2]})
    f_acts = _acts([{"symbol": "F", "ex_date": d[3], "kind": "split", "value": 2.0}])
    assert "split_price_mismatch" in _blocking(fwd, f_acts)
