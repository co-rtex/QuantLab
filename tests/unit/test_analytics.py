import math

import numpy as np
import pandas as pd
import pytest

from quantlab.analytics.performance import (
    compute_performance,
    trade_statistics,
)
from quantlab.analytics.risk import (
    StressScenario,
    apply_stress,
    concentration,
    monte_carlo,
    worst_periods,
)
from quantlab.optimize.allocators import (
    Bounds,
    InfeasibleConstraints,
    OptimizationError,
    mean_variance,
    min_variance,
    risk_contributions,
    risk_parity,
)

IDX = pd.bdate_range("2024-01-01", periods=5)


def test_risk_free_rate_convention():
    e = pd.Series(
        100 * 1.001 ** np.arange(300) * (1 + 0.01 * np.sin(np.arange(300))),
        index=pd.bdate_range("2020-01-01", periods=300),
    )
    m0 = compute_performance(e)
    m5 = compute_performance(e, risk_free_rate=0.05)
    assert m5["sharpe"] < m0["sharpe"]
    r = e.pct_change().dropna()
    rf = 1.05 ** (1 / 252) - 1
    assert m5["sharpe"] == pytest.approx((r - rf).mean() / (r - rf).std(ddof=1) * math.sqrt(252))


def test_undefined_statistics_are_none_with_reason():
    flat = pd.Series([100.0] * 10, index=pd.bdate_range("2024-01-01", periods=10))
    m = compute_performance(flat)
    assert m["sharpe"] is None and "zero volatility" in m.undefined["sharpe"]
    assert m["calmar"] is None
    assert m["sortino"] is None
    single = compute_performance(flat.iloc[:1])
    assert single["sharpe"] is None
    unrec = compute_performance(pd.Series([100, 90, 80.0], index=IDX[:3]))
    assert unrec["max_drawdown_recovery_sessions"] is None


def test_trade_statistics_fifo():
    t = pd.DataFrame(
        [
            {"date": IDX[0], "symbol": "A", "quantity": 10, "price": 10.0, "commission": 0.0},
            {"date": IDX[1], "symbol": "A", "quantity": 10, "price": 20.0, "commission": 0.0},
            {"date": IDX[2], "symbol": "A", "quantity": -15, "price": 15.0, "commission": 0.0},
            {"date": IDX[3], "symbol": "A", "quantity": -5, "price": 10.0, "commission": 0.0},
        ]
    )
    m = trade_statistics(t)
    # round trip 1: 10@10 + 5@20 sold at 15 -> +50 -25 = +25 ; round trip 2: 5@20 sold at 10
    assert m["round_trips"] == 2
    assert m["win_rate"] == 0.5
    assert m["avg_win"] == pytest.approx(25.0)
    assert m["avg_loss"] == pytest.approx(-50.0)
    assert m["profit_factor"] == pytest.approx(0.5)


def test_risk_contributions_sum_to_vol_and_parity_equalizes():
    cov = np.array([[0.04, 0.006, 0.0], [0.006, 0.09, 0.01], [0.0, 0.01, 0.01]])
    w = np.array([0.5, 0.3, 0.2])
    rc = risk_contributions(w, cov)
    assert rc["component"].sum() == pytest.approx(math.sqrt(w @ cov @ w))
    assert rc["pct"].sum() == pytest.approx(1.0)
    rp = risk_parity(cov)
    pct = risk_contributions(rp, cov)["pct"].to_numpy()
    np.testing.assert_allclose(pct, 1 / 3, atol=1e-5)


def test_min_variance_diagonal_closed_form():
    var = np.array([0.04, 0.01, 0.0025])
    w = min_variance(np.diag(var))
    expected = (1 / var) / (1 / var).sum()
    np.testing.assert_allclose(w, expected, atol=1e-6)
    capped = min_variance(np.diag(var), Bounds(0, 0.5))
    assert capped.max() <= 0.5 + 1e-9 and capped.sum() == pytest.approx(1)


def test_mean_variance_and_infeasibility():
    cov = np.diag([0.04, 0.04])
    w = mean_variance(np.array([0.10, 0.02]), cov, risk_aversion=2.0)
    assert w[0] > w[1]
    with pytest.raises(InfeasibleConstraints):
        min_variance(cov, Bounds(0, 0.4))
    with pytest.raises(OptimizationError):
        min_variance(np.array([[1.0, 2.0], [2.0, 1.0]]))  # not PSD
    with pytest.raises(OptimizationError):
        mean_variance(np.array([np.nan, 0.1]), cov)


def test_stress_concentration_and_monte_carlo():
    w = pd.Series({"A": 0.6, "B": 0.4})
    s = StressScenario("crash", {"Technology": -0.3}, market_shock=-0.1)
    out = apply_stress(
        w,
        s,
        betas=pd.Series({"A": 1.5, "B": 0.5}),
        sectors=pd.Series({"A": "Technology", "B": "Energy"}),
    )
    assert out["portfolio_return"] == pytest.approx(0.6 * -0.3 + 0.4 * 0.5 * -0.1)
    c = concentration(w)
    assert c["hhi"] == pytest.approx(0.52) and c["effective_n"] == pytest.approx(1 / 0.52)
    rng = np.random.default_rng(2)
    r = rng.normal(0.0004, 0.01, 1000)
    mc1 = monte_carlo(r, horizon=60, n_paths=500, seed=3)
    mc2 = monte_carlo(r, horizon=60, n_paths=500, seed=3)
    assert mc1 == mc2
    q = mc1["terminal_quantiles"]
    assert q["0.05"] < q["0.5"] < q["0.95"]
    e = pd.Series(
        np.r_[np.linspace(100, 120, 50), np.linspace(120, 90, 30), np.linspace(90, 130, 60)],
        index=pd.bdate_range("2024-01-01", periods=140),
    )
    wp = worst_periods(e, window=10, top=2)
    assert wp.iloc[0]["return"] < 0


def test_trade_statistics_handle_splits_and_unmatched_sells():
    d = pd.bdate_range("2024-01-01", periods=4)
    t = pd.DataFrame(
        [
            {"date": d[0], "symbol": "A", "quantity": 100, "price": 50.0, "commission": 0.0},
            {"date": d[2], "symbol": "A", "quantity": -200, "price": 30.0, "commission": 0.0},
            {"date": d[3], "symbol": "B", "quantity": -5, "price": 10.0, "commission": 0.0},
        ]
    )
    splits = pd.DataFrame([{"date": d[1], "symbol": "A", "ratio": 2.0}])
    m = trade_statistics(t, splits)
    assert m["round_trips"] == 1  # the unmatched B sell is not a round trip
    assert m["avg_win"] == pytest.approx(200 * (30 - 25))  # +1000, not -2000
    rev = pd.DataFrame(
        [
            {"date": d[0], "symbol": "C", "quantity": 100, "price": 10.0, "commission": 0.0},
            {"date": d[2], "symbol": "C", "quantity": -10, "price": 110.0, "commission": 0.0},
        ]
    )
    m2 = trade_statistics(rev, pd.DataFrame([{"date": d[1], "symbol": "C", "ratio": 0.1}]))
    assert m2["avg_win"] == pytest.approx(10 * (110 - 100))


def test_factor_exposures_recover_known_loadings():
    from quantlab.analytics.factors import exposures

    rng = np.random.default_rng(9)
    idx = pd.bdate_range("2020-01-01", periods=750)
    f = pd.DataFrame(
        {"market": rng.normal(0.0004, 0.01, 750), "sector:Tech": rng.normal(0, 0.005, 750)},
        index=idx,
    )
    y = 0.0002 + 1.3 * f["market"] - 0.6 * f["sector:Tech"] + rng.normal(0, 0.002, 750)
    out = exposures(y, f)
    load = {x["factor"]: x for x in out["loadings"]}
    assert load["market"]["beta"] == pytest.approx(1.3, abs=0.03)
    assert load["sector:Tech"]["beta"] == pytest.approx(-0.6, abs=0.05)
    assert out["alpha_annualized"] == pytest.approx(0.0002 * 252, abs=0.03)
    assert out["r_squared"] > 0.9
    assert "error" in exposures(y.iloc[:10], f)


def test_sector_factors_and_analysis_include_exposures(market):
    from quantlab.analytics.factors import sector_factors
    from quantlab.analytics.summary import analyze_backtest
    from quantlab.backtest.engine import BacktestConfig, run_backtest
    from quantlab.strategies.library import create_strategy

    sectors = market.instruments.set_index("symbol")["sector"]
    f = sector_factors(market.bars, market.actions, sectors, "SYNIDX")
    assert "market" in f.columns
    assert any(c.startswith("sector:") for c in f.columns)
    res = run_backtest(
        market.bars,
        market.actions,
        create_strategy("equal_weight"),
        BacktestConfig(start="2020-01-02", benchmark="SYNIDX"),
    )
    doc = analyze_backtest(res, f)["factor_exposures"]
    market_beta = next(x["beta"] for x in doc["loadings"] if x["factor"] == "market")
    assert 0.5 < market_beta < 1.5  # equal weight of beta~1 synthetic names


def test_float_noise_is_not_mistaken_for_dispersion():
    # Compounding at a constant rate: every return is 0.1% up to ~1e-16 of rounding noise.
    idx = pd.bdate_range("2024-01-01", periods=300)
    equity = pd.Series(100 * 1.001 ** np.arange(300), index=idx)
    noise = np.random.default_rng(0).normal(0, 0.01, 300)
    bench = pd.Series(100 * np.cumprod(1 + noise), index=idx)
    m = compute_performance(equity, benchmark=bench)
    for k in ("sharpe", "skewness", "excess_kurtosis", "correlation"):
        assert m.values[k] is None, k
        assert m.undefined[k], k
    assert "variance" in m.undefined["skewness"]


def test_single_return_has_no_sortino():
    m = compute_performance(pd.Series([100.0, 90.0], index=pd.bdate_range("2024-01-01", periods=2)))
    assert m.values["sortino"] is None
    assert m.values["sharpe"] is None


def test_cornish_fisher_var_of_a_constant_return_is_its_negated_mean():
    from quantlab.analytics.risk import cornish_fisher_var

    assert cornish_fisher_var(np.full(30, 0.001)) == pytest.approx(-0.001)
