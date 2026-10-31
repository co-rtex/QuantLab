"""Undefined statistics are ``None`` plus a reason, never 0 or rounding noise (issue #13)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from quantlab.analytics.performance import (
    BENCHMARK_METRICS,
    RETURN_METRICS,
    compute_performance,
)
from quantlab.analytics.risk import risk_summary
from quantlab.analytics.summary import rolling_metrics
from quantlab.backtest.engine import BacktestConfig
from quantlab.data.schema import normalize_actions
from quantlab.research.validation import walk_forward
from tests.conftest import make_bars


def _walk(n: int = 300, seed: int = 0) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.Series(100 * np.cumprod(1 + rng.normal(0.0004, 0.01, n)), index=idx)


def test_index_tracker_has_zero_tracking_error_and_undefined_ir():
    # Equity is a constant multiple of the benchmark: active returns are exactly zero, but
    # floating point leaves ~1e-16 per-period noise. main reported TE 1.3e-15, IR -0.46.
    b = _walk(500)
    m = compute_performance(3.7 * b, b)
    assert m["tracking_error"] == 0.0
    assert m["information_ratio"] is None
    assert m.undefined["information_ratio"] == "zero tracking error"


def test_genuine_small_tracking_error_is_still_measured():
    b = _walk(500)
    rng = np.random.default_rng(7)
    e = b * np.cumprod(1 + rng.normal(0, 1e-5, len(b)))  # 1bp/day of active noise
    m = compute_performance(e, b)
    assert m["tracking_error"] == pytest.approx(1e-5 * math.sqrt(252), rel=0.15)
    assert m["information_ratio"] is not None


@pytest.mark.parametrize("start", [0.0, -100.0])
def test_non_positive_starting_equity_makes_every_metric_undefined(start):
    e = pd.Series([start, 50.0, 60.0, 55.0], index=pd.bdate_range("2024-01-01", periods=4))
    b = pd.Series([1.0, 1.1, 1.2, 1.1], index=e.index)
    m = compute_performance(e, b)
    for key in (*RETURN_METRICS, *BENCHMARK_METRICS):
        assert m[key] is None, key
        assert m.undefined[key] == "initial wealth is not positive", key
    assert m["observations"] == 4


def test_short_sample_makes_every_metric_undefined():
    e = pd.Series([100.0], index=pd.bdate_range("2024-01-01", periods=1))
    m = compute_performance(e, e)
    for key in (*RETURN_METRICS, *BENCHMARK_METRICS):
        assert m[key] is None, key
        assert m.undefined[key] == "fewer than two observations", key


def test_rolling_metrics_use_periods_per_year_and_risk_free_rate():
    e = _walk(300)
    r = e.pct_change()
    w = 63
    rf = 0.05
    out = rolling_metrics(e, w, risk_free_rate=rf, periods_per_year=52)
    rf_p = (1 + rf) ** (1 / 52) - 1
    tail = r.iloc[-w:]
    expected_sharpe = (tail.mean() - rf_p) / tail.std(ddof=1) * math.sqrt(52)
    assert out["rolling_sharpe"].iloc[-1] == pytest.approx(expected_sharpe, rel=1e-12)
    assert out["rolling_vol"].iloc[-1] == pytest.approx(tail.std(ddof=1) * math.sqrt(52))


def test_rolling_sharpe_is_undefined_on_flat_windows():
    # 80 flat sessions, then 80 of constant +0.1% growth: both have zero dispersion of
    # returns, so Sharpe is undefined on both; main produced 0/0 noise or ~1e14 values.
    growth = [100.0 * 1.001**k for k in range(1, 81)]
    noisy = list(_walk(40, seed=3) * growth[-1] / 100)
    e = pd.Series([100.0] * 80 + growth + noisy, index=pd.bdate_range("2024-01-01", periods=200))
    out = rolling_metrics(e, 20)
    # Windows lying wholly inside one stretch (straddling windows do have dispersion).
    flat, steady = out.loc[e.index[20] : e.index[79]], out.loc[e.index[99] : e.index[159]]
    assert flat["rolling_sharpe"].isna().all()
    assert steady["rolling_sharpe"].isna().all()
    assert (flat["rolling_vol"] == 0.0).all()
    assert np.isfinite(out["rolling_sharpe"].iloc[-1])  # defined again once returns vary


def test_risk_summary_annualizes_with_periods_per_year():
    r = _walk(300).pct_change().dropna()
    out = risk_summary(r, periods_per_year=52)
    assert out["annualized_volatility"] == pytest.approx(r.std(ddof=1) * math.sqrt(52))
    assert out["periods_per_year"] == 52


def test_walk_forward_with_every_fold_failing_returns_an_undefined_result():
    # A trades only for the first 62 sessions; every out-of-sample window starts later,
    # so each OOS backtest fails. main raised "No objects to concatenate".
    n = 400
    a = [100.0 + 0.1 * k for k in range(62)] + [None] * (n - 62)
    bars = make_bars({"A": a, "B": [10.0 + 0.01 * k for k in range(n)]})
    cfg = BacktestConfig(universe=["A"], rebalance="monthly")
    out = walk_forward(
        bars,
        normalize_actions(None),
        "buy_and_hold",
        {},
        cfg,
        train_sessions=60,
        test_sessions=40,
    )
    assert out["error"] == "every out-of-sample fold failed"
    assert len(out["folds"]) >= 1
    assert all(f.get("error") for f in out["folds"])
    assert out["mean_oos_sharpe"] is None
