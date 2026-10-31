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
    assert placed.loc[("A", d[3] + pd.Timedelta(days=9)), "placement"] == "outside_history"
    assert placed.loc[("ZZZ", d[1]), "placement"] == "unknown_symbol"
    assert (
        placed.loc[placed["placement"].isin(["outside_history", "unknown_symbol"]), "row"] == -1
    ).all()


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
        # The audit defect: the split row is missing, so the data shows a 50% loss.
        "missing_split": (bars, _acts([]), "split_like_returns"),
        # A split recorded one session late leaves the drop unexplained on its true date.
        "late_split": (
            bars,
            _acts([{**split, "ex_date": d[6]}]),
            "split_like_returns",
        ),
        "wrong_ratio": (bars, _acts([{**split, "value": 3.0}]), "split_price_mismatch"),
        "phantom_split": (
            bars,
            _acts([split, {**split, "ex_date": d[2]}]),
            "split_price_mismatch",
        ),
        "duplicate_action": (bars, _acts([split, split]), "duplicate_corporate_actions"),
        "orphan_symbol": (
            bars,
            _acts([split, {**split, "symbol": "ZZZ"}]),
            "orphan_corporate_actions",
        ),
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
            "corporate_actions_outside_history",
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
            "corporate_actions_outside_history",
        ),
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
    assert "A" in msg or "ZZZ" in msg


def test_genuine_large_move_is_a_warning_not_a_gate():
    # +80% (e.g. a takeover bid) is not a split signature and must not block ingestion.
    bars = make_bars({"A": [100.0] * 4 + [180.0] * 4})
    report = assess_quality(bars, _acts([]))
    by = {c.name: c for c in report.checks}
    assert by["extreme_returns"].status == "warn"
    assert by["split_like_returns"].status == "pass"
    enforce_gates(report)


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
