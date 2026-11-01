"""A backtest that cannot fit the worker's memory is refused at submission (#163).

The estimate is checked against the committed raw measurements of the real worker
container (``benchmarks/results/*-worker-memory-after-*.json``; synthetic data).
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pytest

from quantlab.data.calendar import trading_days
from quantlab_api.config import get_settings
from quantlab_api.errors import ApiError
from quantlab_api.limits import BacktestCost, backtest_cost, check_backtest_memory

RESULTS = Path(__file__).resolve().parents[2] / "benchmarks" / "results"
LIMIT_MB = 1536.0  # the compose worker's memory limit the sweep ran under
START, END = date(2005, 1, 3), date(2023, 12, 29)


def _measured() -> list[dict]:
    rows = []
    for path in sorted(RESULTS.glob("*-worker-memory-after-*.json")):
        doc = json.loads(path.read_text())
        assert doc["data_is_synthetic"] and doc["contended"] is False, path.name
        rows += doc["rows"]
    assert len(rows) == 15
    return rows


def _cost(row: dict) -> BacktestCost:
    return backtest_cost(row["universe"], START, END, START, None, row["rebalance"])


def test_the_window_counts_sessions_and_rebalances():
    daily, weekly, monthly = (
        backtest_cost(100, START, END, START, None, f) for f in ("daily", "weekly", "monthly")
    )
    assert daily.sessions == weekly.sessions == monthly.sessions == 4785
    assert (daily.rebalances, weekly.rebalances, monthly.rebalances) == (4785, 992, 229)
    # Rebalances count over the traded window; sessions include the history before it.
    later = backtest_cost(100, START, END, date(2020, 1, 2), None, "monthly")
    assert later.sessions == 4785 and later.rebalances == 49  # 48 month-ends + the start
    # An end inside the data shortens both; one after it changes nothing.
    short = backtest_cost(100, START, END, START, date(2009, 12, 31), "daily")
    assert short.sessions == len(trading_days(START, date(2009, 12, 31))) < 4785
    assert backtest_cost(100, START, END, START, date(2030, 1, 1), "daily").sessions == 4785


def test_the_estimate_covers_every_measured_job_clear_of_the_limit():
    for row in _measured():
        if row["anon_peak_mb"] >= 0.95 * LIMIT_MB:
            continue  # at the limit: these may only be rejected (next test)
        assert _cost(row).memory_mb >= row["anon_peak_mb"], row


def test_jobs_measured_at_the_limit_are_refused_and_the_boundary_falls_where_measured():
    limit = get_settings().max_backtest_memory_mb
    assert limit == math.floor(0.8 * LIMIT_MB)  # 20% headroom over the envelope
    by = {(r["universe"], r["rebalance"], r["strategy"]): r for r in _measured()}
    for key, row in by.items():
        refused = _cost(row).memory_mb > limit
        if row["anon_peak_mb"] >= 0.95 * LIMIT_MB:
            assert refused, key  # 300 and 400 symbols daily
    assert _cost(by[(400, "weekly", "equal_weight")]).memory_mb <= limit  # admitted
    assert _cost(by[(200, "daily", "equal_weight")]).memory_mb > limit  # just over


def test_a_refusal_names_the_estimate_and_the_limit():
    cost = backtest_cost(300, START, END, START, None, "daily")
    with pytest.raises(ApiError) as exc:
        check_backtest_memory(cost, 1228)
    e = exc.value
    assert (e.status, e.code) == (422, "backtest_too_large")
    assert "1228 MB limit" in e.message and f"{cost.memory_mb:.0f} MB" in e.message
    assert e.details["estimated_mb"] == round(cost.memory_mb, 1) and e.details["limit_mb"] == 1228
    check_backtest_memory(cost, math.ceil(cost.memory_mb))  # at the estimate: admitted


@pytest.mark.parametrize(
    ("symbols", "start", "end", "rebalance"),
    [
        (1500, date(2022, 1, 3), date(2023, 12, 29), "daily"),  # wide and short
        (110, date(1990, 1, 2), date(2023, 12, 29), "daily"),  # a long history
        (500, START, END, "monthly"),  # more symbols than were measured
    ],
)
def test_backtests_outside_the_measured_range_are_refused(symbols, start, end, rebalance):
    # #174 review: the estimate alone would admit these, but it was never measured there.
    cost = backtest_cost(symbols, start, end, start, None, rebalance)
    assert cost.memory_mb <= 1228
    with pytest.raises(ApiError) as exc:
        check_backtest_memory(cost, 1228)
    assert exc.value.code == "backtest_too_large"
    assert exc.value.details["reason"] == "outside_measured_range"
    assert "larger backtests are refused" in exc.value.message


def test_the_effective_caps_for_2005_to_2023():
    # The most symbols a 2005-2023 backtest may have under the default limit, per
    # rebalance frequency: the memory estimate for daily, the measured range otherwise.
    def admitted(n: int, freq: str) -> bool:
        try:
            check_backtest_memory(backtest_cost(n, START, END, START, None, freq), 1228)
        except ApiError:
            return False
        return True

    freqs = ("daily", "weekly", "monthly", "quarterly")
    caps = {f: max(n for n in range(1, 1501) if admitted(n, f)) for f in freqs}
    assert caps == {"daily": 199, "weekly": 400, "monthly": 400, "quarterly": 400}


def test_bar_dates_counts_distinct_dates_including_non_trading_days(tmp_path):
    import pandas as pd

    from quantlab_api.marketdata import bar_dates

    saturday = pd.Timestamp("2024-01-06")  # not a session, but a bar the engine steps through
    days = [*trading_days("2024-01-02", "2024-01-31"), saturday]
    rows = [{"symbol": s, "date": d} for s in ("A", "B") for d in days]
    pd.DataFrame(rows).to_parquet(tmp_path / "bars.parquet", index=False)
    assert bar_dates(tmp_path) == len(trading_days("2024-01-02", "2024-01-31")) + 1
    assert (
        bar_dates(tmp_path, date(2024, 1, 10)) == len(trading_days("2024-01-02", "2024-01-10")) + 1
    )


def test_sessions_are_the_versions_bar_dates_when_there_are_more():
    # The engine steps through distinct bar dates; a bar on a non-trading day only warns.
    calendar = backtest_cost(100, START, END, START, None, "daily")
    extra = backtest_cost(100, START, END, START, None, "daily", bar_dates=4790)
    fewer = backtest_cost(100, START, END, START, None, "daily", bar_dates=4000)
    assert (calendar.sessions, extra.sessions, fewer.sessions) == (4785, 4790, 4785)
    with pytest.raises(ApiError):  # 4,790 sessions is outside the measured range
        check_backtest_memory(extra, 10_000)
