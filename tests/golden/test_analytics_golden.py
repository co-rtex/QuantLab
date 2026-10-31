"""Performance and risk statistics against values computed by hand (golden)."""

import math

import numpy as np
import pandas as pd
import pytest

from quantlab.analytics.performance import compute_performance, drawdown_episodes
from quantlab.analytics.risk import gaussian_var, historical_es, historical_var

IDX = pd.bdate_range("2024-01-01", periods=5)


def test_golden_returns_and_drawdown():
    e = pd.Series([100.0, 110.0, 99.0, 108.9, 121.0], index=IDX)
    m = compute_performance(e)
    assert m["total_return"] == pytest.approx(0.21)
    assert m["max_drawdown"] == pytest.approx(99 / 110 - 1)
    assert m["cagr"] == pytest.approx(1.21 ** (252 / 4) - 1)
    r = np.array([0.1, -0.1, 0.1, 121 / 108.9 - 1])
    assert m["volatility"] == pytest.approx(r.std(ddof=1) * math.sqrt(252))
    assert m["sharpe"] == pytest.approx(r.mean() / r.std(ddof=1) * math.sqrt(252))
    dd = np.minimum(r, 0)
    assert m["sortino"] == pytest.approx(r.mean() / np.sqrt(np.mean(dd**2)) * math.sqrt(252))
    eps = drawdown_episodes(e)
    assert eps.iloc[0]["peak"] == IDX[1] and eps.iloc[0]["trough"] == IDX[2]
    assert eps.iloc[0]["recovery"] == IDX[4]
    assert eps.iloc[0]["recovery_sessions"] == 2


def test_benchmark_relative_golden():
    rng = np.random.default_rng(0)
    rb = rng.normal(0.0005, 0.01, 500)
    rp = 0.0002 + 1.5 * rb + rng.normal(0, 0.002, 500)
    idx = pd.bdate_range("2020-01-01", periods=501)
    b = pd.Series(100 * np.r_[1, np.cumprod(1 + rb)], index=idx)
    p = pd.Series(100 * np.r_[1, np.cumprod(1 + rp)], index=idx)
    m = compute_performance(p, b)
    beta_ref = np.cov(rp, rb, ddof=1)[0, 1] / np.var(rb, ddof=1)
    assert m["beta"] == pytest.approx(beta_ref)
    assert m["alpha"] == pytest.approx(252 * (rp.mean() - beta_ref * rb.mean()))
    te = (rp - rb).std(ddof=1) * math.sqrt(252)
    assert m["tracking_error"] == pytest.approx(te)
    assert m["information_ratio"] == pytest.approx((rp - rb).mean() * 252 / te)
    same = compute_performance(b, b)
    assert same["beta"] == pytest.approx(1.0) and same["information_ratio"] is None


def test_var_es_golden_and_ordering():
    r = np.linspace(-0.05, 0.05, 101)  # uniform grid incl. -0.05..0.05
    assert historical_var(r, 0.95) == pytest.approx(0.045)
    assert historical_es(r, 0.95) == pytest.approx(np.mean(np.linspace(-0.05, -0.045, 6)) * -1)
    rng = np.random.default_rng(1)
    x = rng.normal(0, 0.01, 100_000)
    assert gaussian_var(x, 0.99) == pytest.approx(0.02326, rel=0.02)
    assert historical_var(x, 0.99) == pytest.approx(0.02326, rel=0.03)
    assert historical_es(x, 0.99) >= historical_var(x, 0.99)
