"""The optimization job's memory admission (#224): an envelope over measured jobs."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from quantlab_api.errors import ApiError
from quantlab_api.limits import (
    OPTIMIZE_MEASURED_MAX_SYMBOL_SESSIONS,
    OptimizeCost,
    check_optimize_memory,
    optimize_cost,
)

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "benchmarks" / "results" / "20261101T1700Z-optimize-memory-8afffa4.json"
LIMIT = 1228  # the default max_backtest_memory_mb: the worker's budget for one job


def test_the_envelope_covers_every_measured_job() -> None:
    rows = json.loads(RESULTS.read_text())["rows"]
    assert len(rows) == 9 and all(r["oom_kills"] == 0 for r in rows)
    for r in rows:
        cost = OptimizeCost(r["symbols"] + 1, r["sessions"])  # and the benchmark
        assert cost.symbols * cost.sessions == r["symbol_sessions"]
        assert cost.memory_mb >= r["anon_peak_mb"], r
    assert max(r["symbol_sessions"] for r in rows) == OPTIMIZE_MEASURED_MAX_SYMBOL_SESSIONS


def test_the_largest_measured_job_is_admitted_and_one_more_session_is_not() -> None:
    check_optimize_memory(OptimizeCost(101, 7056), LIMIT)
    with pytest.raises(ApiError) as exc:
        check_optimize_memory(OptimizeCost(101, 7057), LIMIT)
    err = exc.value
    assert err.status == 422 and err.code == "optimization_too_large"
    assert err.details["reason"] == "outside_measured_range"
    assert err.details["sessions"] == 7057


def test_an_estimate_over_the_limit_is_refused_and_never_shown_under_it() -> None:
    cost = OptimizeCost(101, 7056)
    limit = int(cost.memory_mb)  # just under the estimate
    with pytest.raises(ApiError) as exc:
        check_optimize_memory(cost, limit)
    details = exc.value.details
    assert details["reason"] == "estimated_memory" and details["limit_mb"] == limit
    assert details["estimated_mb"] > limit and details["estimated_mb"] >= cost.memory_mb


def test_within_the_measured_range_the_default_budget_never_refuses() -> None:
    assert OptimizeCost(101, 7056).memory_mb < LIMIT


@pytest.mark.parametrize(
    ("as_of", "sessions"),
    [
        (None, 756),  # the data's last session
        (date(2023, 6, 30), 756),  # after the data: clamped
        (date(2020, 12, 31), 253),  # the 2020 sessions only
    ],
)
def test_sessions_run_from_the_data_start_to_as_of(as_of: date | None, sessions: int) -> None:
    cost = optimize_cost(9, date(2020, 1, 2), date(2022, 12, 30), as_of)
    assert (cost.symbols, cost.sessions) == (9, sessions)
