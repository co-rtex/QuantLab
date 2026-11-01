"""Adversarial and property tests for the backtest admission bound (#181). Synthetic data.

The bound must neither refuse valid research nor admit a job larger than estimated. These
tests check its inputs against what the engine will actually run: the instruments (a
reused ticker, a renamed one, a universe with duplicates) and the sessions and rebalances
(a version with bars on days the exchange calendar does not trade).
"""

from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from quantlab.backtest.engine import BacktestConfig, rebalance_mask, run_backtest
from quantlab.data.calendar import trading_days
from quantlab.data.schema import normalize_actions, normalize_bars
from quantlab.strategies.library import create_strategy
from quantlab_api.errors import ApiError
from quantlab_api.limits import (
    BACKTEST_BASE_MB,
    BacktestCost,
    backtest_cost,
    check_backtest_memory,
    universe_width,
)

START, END = date(2005, 1, 3), date(2023, 12, 29)
FREQS = ("daily", "weekly", "monthly", "quarterly")
LIMIT = 1228


def _admitted(cost: BacktestCost, limit: int = LIMIT) -> bool:
    try:
        check_backtest_memory(cost, limit)
    except ApiError:
        return False
    return True


# --- the instruments the job runs -------------------------------------------------------


def test_a_reused_ticker_counts_once_per_holder():
    # A: BBB was reused by a second company. Without a universe the job runs all three
    # instruments, so three are counted, not two tickers.
    catalog = [("AAA", "QL1"), ("BBB", "QL2"), ("BBB", "QL3")]
    assert universe_width(catalog, None) == 3


def test_a_renamed_instrument_counts_once():
    assert universe_width([("OLD", "QL1"), ("NEW", "QL1"), ("CCC", "QL2")], None) == 2


def test_a_version_without_identity_counts_tickers():
    assert universe_width([("AAA", None), ("BBB", None)], None) == 2


def test_a_universe_with_duplicates_counts_each_ticker_once():
    # C: the engine deduplicates the universe, so 300 tickers listed twice are 300.
    assert universe_width([], ["aaa", "AAA", " bbb", "BBB"]) == 2
    wanted = [f"S{i:03d}" for i in range(300)] * 2
    assert universe_width([], wanted) == 300
    assert _admitted(backtest_cost(universe_width([], wanted), START, END, START, None, "monthly"))


# --- sessions and rebalances: what the engine steps through -----------------------------


def _engine_decisions(days: pd.DatetimeIndex, start: pd.Timestamp | None, freq: str) -> int:
    """Rebalance decisions the engine makes on one always-tradable symbol over ``days``."""
    bars = normalize_bars(
        pd.DataFrame(
            {
                "symbol": "AAA",
                "date": days,
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "volume": 1e4,
            }
        )
    )
    cfg = BacktestConfig(start=start.date() if start is not None else None, rebalance=freq)
    res = run_backtest(bars, normalize_actions(None), create_strategy("equal_weight"), cfg)
    return res.weights_target["date"].nunique()


def test_seven_day_week_data_counts_the_rebalances_the_engine_makes():
    # B: a CSV with a bar every day, 2005-2023. The engine rebalances daily on all 6,935
    # dates; the calendar has 4,785 sessions.
    every_day = pd.date_range(START, END, freq="D")
    cost = backtest_cost(1, START, END, None, None, "daily", every_day)
    assert (cost.sessions, cost.rebalances) == (6935, 6935)
    assert cost.rebalances == int(rebalance_mask(every_day, "daily").sum())
    for freq in ("weekly", "monthly", "quarterly"):
        assert backtest_cost(1, START, END, None, None, freq, every_day).rebalances == int(
            rebalance_mask(every_day, freq).sum()
        )


def test_seven_day_week_data_lowers_the_daily_cap():
    # The daily cap over 2005-2023 on such data was 172 symbols, estimated with the
    # calendar's 4,785 rebalances: 1,227 MB as admitted, but 1,439 MB on the engine's
    # 6,935. With the engine's count it is 137.
    every_day = pd.date_range(START, END, freq="D")

    def cap() -> int:
        return max(
            s
            for s in range(1, 401)
            if _admitted(backtest_cost(s, START, END, None, None, "daily", every_day))
        )

    assert cap() == 137
    assert backtest_cost(172, START, END, None, None, "daily", every_day).memory_mb > LIMIT


@st.composite
def _window(draw):
    first = draw(st.dates(date(2015, 1, 1), date(2022, 6, 30)))
    calendar = trading_days(
        first, pd.Timestamp(first) + pd.Timedelta(days=draw(st.integers(40, 500)))
    )
    days = set(calendar)
    if draw(st.booleans()):  # bars on weekends, as a 7-day-week CSV has
        weekends = [d for d in pd.date_range(calendar[0], calendar[-1]) if d.weekday() >= 5]
        days |= set(draw(st.lists(st.sampled_from(weekends), max_size=40))) if weekends else set()
    exact = len(days) >= len(calendar)  # bars on every calendar session (and maybe more)
    if draw(st.booleans()):  # or gaps: sessions without any bar
        gone = draw(st.lists(st.sampled_from(list(calendar[1:-1])), max_size=20))
        days -= set(gone)
        exact = exact and not gone
    days = pd.DatetimeIndex(sorted(days))
    start = draw(st.one_of(st.none(), st.sampled_from(list(days[: len(days) // 2]))))
    return days, calendar, start, exact


@settings(max_examples=30, deadline=None)
@given(window=_window(), freq=st.sampled_from(FREQS))
def test_the_estimate_counts_at_least_the_engines_rebalances(window, freq):
    # E: the estimate uses the engine's own rule (rebalance_mask) on the larger of the
    # calendar and the bar dates. The engine decides on every masked session but the last.
    days, calendar, start, exact = window
    first, last = calendar[0].date(), calendar[-1].date()
    cost = backtest_cost(
        1, first, last, start.date() if start is not None else None, None, freq, days
    )
    made = _engine_decisions(days, start, freq) + 1
    assert cost.rebalances >= made
    assert cost.sessions >= len(days)
    if exact:  # bars on every session: the counts are the engine's exactly
        assert (cost.rebalances, cost.sessions) == (made, len(days))


# --- monotonicity ------------------------------------------------------------------------


@settings(max_examples=60, deadline=None)
@given(
    symbols=st.integers(1, 399),
    first=st.dates(date(1995, 1, 1), date(2022, 1, 1)),
    years=st.integers(1, 25),
    freq=st.sampled_from(FREQS),
)
def test_the_estimate_is_monotone_in_symbols_and_history(symbols, first, years, freq):
    end = min(date(first.year + years, 12, 31), END)
    base = backtest_cost(symbols, first, end, None, None, freq)
    assert backtest_cost(symbols + 1, first, end, None, None, freq).memory_mb >= base.memory_mb
    longer = backtest_cost(symbols, date(first.year - 1, 1, 2), end, None, None, freq)
    assert longer.memory_mb >= base.memory_mb
    # Admission is monotone too: a smaller job of an admitted shape is admitted.
    if _admitted(base):
        assert _admitted(backtest_cost(max(1, symbols - 1), first, end, None, None, freq))


@settings(max_examples=60, deadline=None)
@given(first=st.dates(date(2000, 1, 1), date(2023, 1, 1)), sessions=st.integers(63, 3000))
def test_rebalances_are_monotone_in_frequency_over_a_quarter_or_more(first, sessions):
    calendar = trading_days(first, pd.Timestamp(first) + pd.Timedelta(days=sessions * 2))
    calendar = calendar[:sessions]
    counts = [
        backtest_cost(1, calendar[0].date(), calendar[-1].date(), None, None, f).rebalances
        for f in FREQS
    ]
    assert counts == sorted(counts, reverse=True), counts


def test_rebalances_are_not_monotone_in_frequency_over_a_few_days():
    # Why the property above needs a quarter: Mon 2025-09-29 to Thu 2025-10-02 has one week
    # but a month-end inside it. Harmless, as the estimate uses the engine's own count.
    weekly, monthly = (
        backtest_cost(1, date(2025, 9, 29), date(2025, 10, 2), None, None, f).rebalances
        for f in ("weekly", "monthly")
    )
    assert (weekly, monthly) == (2, 3)


# --- no fail-open edge cases ------------------------------------------------------------


@pytest.mark.parametrize(
    ("data_start", "data_end", "start", "end"),
    [
        (START, END, None, date(2004, 6, 30)),  # an end before the data: nothing loaded
        (START, END, date(2024, 3, 1), None),  # a start after the data: nothing traded
    ],
)
def test_degenerate_windows_are_bounded(data_start, data_end, start, end):
    # The job then fails cleanly ("no market data", "fewer than two sessions"); the
    # estimate stays bounded by what the data holds, so nothing large is admitted.
    cost = backtest_cost(400, data_start, data_end, start, end, "daily")
    assert cost.rebalances == 0
    assert cost.memory_mb <= backtest_cost(400, data_start, data_end, None, None, "daily").memory_mb


def test_a_version_without_dates_is_empty():
    # D (not a defect): the route skips the check for a version without start and end
    # dates. Those come from the manifest's first and last bar, so only a version without
    # bars lacks them, and its job fails at once ("no market data"); nothing is loaded.
    assert BacktestCost(400, 0, 0).memory_mb == BACKTEST_BASE_MB


# --- exact boundaries and consistent refusals --------------------------------------------


def test_the_memory_boundary_is_exact_and_the_refusal_says_over():
    # 1228.017 MB used to read "needs an estimated 1228 MB, over the 1228 MB limit", with
    # estimated_mb 1228.0: the figure shown is now rounded up, so it is over the limit.
    cost = BacktestCost(200, 4783, 4785)
    assert 1228 < cost.memory_mb < 1228.05
    with pytest.raises(ApiError) as exc:
        check_backtest_memory(cost, LIMIT)
    d = exc.value.details
    assert d["estimated_mb"] == 1228.1 and d["estimated_mb"] > d["limit_mb"] == LIMIT
    assert "needs an estimated 1228.1 MB, over the 1228 MB limit" in exc.value.message
    assert _admitted(cost, math.ceil(cost.memory_mb))  # a limit at the estimate admits it


@settings(max_examples=200, deadline=None)
@given(
    symbols=st.integers(1, 400),
    sessions=st.integers(1, 4785),
    rebalances=st.integers(1, 4785),
    limit=st.integers(400, 2000),
)
def test_every_refusal_is_internally_consistent(symbols, sessions, rebalances, limit):
    cost = BacktestCost(symbols, sessions, rebalances)
    if _admitted(cost, limit):
        assert cost.memory_mb <= limit
        assert symbols * sessions <= 400 * 4785 and symbols * rebalances <= 400 * 4785
        return
    with pytest.raises(ApiError) as caught:
        check_backtest_memory(cost, limit)
    exc, d = caught.value, caught.value.details
    assert (exc.status, exc.code) == (422, "backtest_too_large")
    assert (d["symbols"], d["sessions"], d["rebalances"]) == (symbols, sessions, rebalances)
    if d["reason"] == "estimated_memory":
        assert cost.memory_mb > limit
        assert 0 <= d["estimated_mb"] - cost.memory_mb < 0.1 + 1e-9
        assert d["estimated_mb"] > d["limit_mb"] == limit
        assert f"{d['estimated_mb']:.1f} MB, over the {limit} MB limit" in exc.message
    else:
        assert d["reason"] == "outside_measured_range"
        assert (
            symbols > d["max_symbols"]
            or symbols * sessions > d["max_symbol_sessions"]
            or symbols * rebalances > d["max_symbol_rebalances"]
        )
