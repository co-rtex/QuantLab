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
