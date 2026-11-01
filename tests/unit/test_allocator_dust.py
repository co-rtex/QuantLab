"""SLSQP residue at a zero lower bound is set to exactly 0 (#227, engine 1.7.0).

Synthetic data only. Markets 3, 11 and 29 of the issue: generate_market(n_assets=30,
2019-01-02 to 2021-12-31).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quantlab.analytics.risk import covariance
from quantlab.backtest.engine import BacktestConfig, run_backtest
from quantlab.data.adjust import total_returns
from quantlab.data.synthetic import BENCHMARK_SYMBOL, SyntheticSpec, generate_market
from quantlab.optimize import allocators
from quantlab.optimize.allocators import DUST_WEIGHT, Bounds, drop_dust, mean_variance, min_variance
from quantlab.strategies.library import create_strategy

MARKETS = (3, 11, 29)
# Measured on these markets (#227): the residue is at most 8.1e-15 and real weights at least
# 2.3e-6. The band asserted empty leaves DUST_WEIGHT (1e-10) three decades on each side.
GAP = (1e-13, 1e-7)


def _market(seed: int):
    return generate_market(
        SyntheticSpec(n_assets=30, start="2019-01-02", end="2021-12-31", seed=seed)
    )


def _windows(seed: int, lookback: int, every: int = 10):
    m = _market(seed)
    rets = total_returns(m.bars, m.actions).drop(columns=[BENCHMARK_SYMBOL])
    for end in range(lookback + 1, len(rets) + 1, every):  # row 0 has no return
        w = rets.iloc[end - lookback : end]
        w = w[[s for s in w.columns if w[s].notna().all()]]
        if w.shape[1] >= 2:
            yield w


def _raw_weights(monkeypatch) -> dict[str, np.ndarray]:
    """Allocator output before drop_dust, for the strategy's and the optimize API's inputs."""
    monkeypatch.setattr(allocators, "DUST_WEIGHT", 0.0)
    out: dict[str, list[np.ndarray]] = {"strategy": [], "api_min": [], "api_mean": []}
    for seed in MARKETS:
        for w in _windows(seed, 126):  # MinimumVariance defaults: sample covariance
            cap = max(0.25, 1.0 / w.shape[1])
            out["strategy"].append(min_variance(w.cov(ddof=1).to_numpy() * 252, Bounds(0, cap)))
        for w in _windows(seed, 252, every=20):  # the optimize API's defaults
            cov = covariance(w, method="ledoit_wolf").to_numpy()
            cap = max(0.4, 1.0 / w.shape[1])
            out["api_min"].append(min_variance(cov, Bounds(0, cap)))
            out["api_mean"].append(
                mean_variance(w.mean().to_numpy() * 252, cov, 5.0, Bounds(0, cap))
            )
    return {k: np.concatenate(v) for k, v in out.items()}


def test_solver_residue_and_real_weights_are_separated_by_an_empty_gap(monkeypatch):
    for name, w in _raw_weights(monkeypatch).items():
        inside = w[(w >= GAP[0]) & (w < GAP[1])]
        assert len(inside) == 0, (name, sorted(inside)[:5])
        assert (w[w > 0] < GAP[0]).any(), name  # residue does occur: the test is not vacuous


def test_no_allocator_weight_is_left_between_zero_and_the_threshold():
    for seed in MARKETS[:1]:
        for w in _windows(seed, 126, every=20):
            out = min_variance(w.cov(ddof=1).to_numpy() * 252, Bounds(0, 0.25))
            assert not ((out > 0) & (out < DUST_WEIGHT)).any()


def test_drop_dust_zeroes_only_the_dust_and_never_renormalizes():
    w = np.array([0.6, 0.4 - 3e-15, 3e-15, 5e-17])
    out = drop_dust(w, Bounds(0, 1))
    assert list(out) == [0.6, 0.4 - 3e-15, 0.0, 0.0]  # the kept weights are bit-identical
    assert 1 - out.sum() < len(w) * DUST_WEIGHT  # the dropped mass stays in cash
    floored = np.array([0.5, 0.5 - 1e-11, 1e-11])
    assert drop_dust(floored, Bounds(1e-11, 1)) is floored  # a positive floor is respected


@pytest.mark.parametrize("seed", [3])
def test_a_large_account_places_no_dust_orders_from_zero_holdings(seed):
    # With the residue kept (1.6.0's weights) this run has 890 dust decisions and 264 fills
    # under $0.01, each paying commission_min 1 (#227).
    m = _market(seed)
    cfg = BacktestConfig(
        rebalance="weekly",
        fractional_shares=True,
        initial_capital=3e9,
        commission_bps=1,
        commission_min=1,
        slippage_bps=5,
        benchmark=BENCHMARK_SYMBOL,
    )
    res = run_backtest(
        m.bars, m.actions, create_strategy("min_variance", {}), cfg, instruments=m.master
    )
    d = res.weights_target
    assert not ((d["weight"] > 0) & (d["weight"] < DUST_WEIGHT)).any()
    t = res.trades
    notional = (t["quantity"].abs() * t["price"]).to_numpy()
    assert (notional >= 0.01).all(), pd.Series(notional).nsmallest(3).tolist()
