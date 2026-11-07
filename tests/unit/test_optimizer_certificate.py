"""A failed SLSQP solve is accepted only when its point is certified optimal (#253).

SLSQP ends many mean_variance solves at the optimize API's defaults (100 symbols) with mode 8,
"Positive directional derivative for linesearch", at the optimum: its ftol of 1e-15 is
below what double precision resolves for the scaled O(1) objective. The allocator now
accepts such a point only if it is feasible and its Frank-Wolfe gap, an upper bound on
f(w) - f*, is at most OPTIMALITY_GAP. Successful solves are unchanged.

Every reference here is independent of SLSQP: an exact LP for the cheapest vertex, a
closed-form optimum for diagonal problems, and an accelerated projected-gradient solver with
an exact projection for the realistic ones. Synthetic data only.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.optimize import linprog

from quantlab.analytics.risk import covariance
from quantlab.data.adjust import total_returns
from quantlab.data.synthetic import BENCHMARK_SYMBOL, SyntheticSpec, generate_market
from quantlab.optimize import allocators
from quantlab.optimize.allocators import (
    Bounds,
    OptimizationError,
    mean_variance,
    min_variance,
    optimality_gap,
)


def _scaled(cov: np.ndarray, mu: np.ndarray | None, ra: float):
    """The problem _solve_qp solves, built independently from its documented scaling."""
    n = cov.shape[0]
    lin = np.zeros(n) if mu is None else mu
    s = max(float(np.trace(cov)) / n * ra, float(np.abs(lin).max()), 1e-12)
    return ra * cov / s, lin / s


def _project(v: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Exact Euclidean projection onto the capped simplex, by the breakpoints of the shift
    (sum clip(v - t, lo, hi) is piecewise linear and non-increasing in t)."""
    knots = np.sort(np.concatenate([v - lo, v - hi]))
    total = np.clip(v[None, :] - knots[:, None], lo, hi).sum(axis=1)
    k = int(np.searchsorted(-total, -1.0))  # first knot with total <= 1
    if k == 0:
        return np.clip(v - knots[0], lo, hi)
    a, b = knots[k - 1], knots[k]
    ta, tb = total[k - 1], total[k]
    t = a if ta == tb else a + (ta - 1.0) * (b - a) / (ta - tb)
    return np.clip(v - t, lo, hi)


def _reference(q: np.ndarray, c: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """FISTA with adaptive restart, to a Frank-Wolfe gap under 1e-13."""
    lip = float(np.linalg.eigvalsh(q).max())
    x = _project(np.full(len(c), 1 / len(c)), lo, hi)
    y, t = x.copy(), 1.0
    for k in range(100_000):
        xn = _project(y - (q @ y - c) / lip, lo, hi)
        if (q @ y - c) @ (xn - x) > 0:  # restart when momentum points uphill
            y, t = x.copy(), 1.0
            continue
        tn = 0.5 * (1 + math.sqrt(1 + 4 * t * t))
        y, x, t = xn + (t - 1) / tn * (xn - x), xn, tn
        if k % 25 == 0 and optimality_gap(x, q, c, Bounds(lo, hi)) < 1e-13:
            break
    return x


def _f(q: np.ndarray, c: np.ndarray, w: np.ndarray) -> float:
    return float(0.5 * w @ q @ w - c @ w)


# --- the certificate itself -------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "lo", "hi"), [(5, 0.0, 1.0), (12, 0.0, 0.25), (40, 0.01, 0.1), (7, 0.1, 0.4)]
)
def test_the_cheapest_vertex_is_the_lp_optimum(n, lo, hi):
    rng = np.random.default_rng(n)
    for _ in range(20):
        g = rng.standard_normal(n)
        s = allocators._cheapest_vertex(g, Bounds(lo, hi))
        lp = linprog(g, A_eq=np.ones((1, n)), b_eq=[1.0], bounds=[(lo, hi)] * n, method="highs")
        assert lp.status == 0
        assert abs(s.sum() - 1) < 1e-12 and (s >= lo - 1e-15).all() and (s <= hi + 1e-15).all()
        assert g @ s == pytest.approx(lp.fun, abs=1e-12)


def _diagonal_optimum(d: np.ndarray, c: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """min 0.5 sum d_i w_i^2 - c_i w_i on the capped simplex: w_i = clip((c_i - lam) / d_i)
    for the lam that makes the weights sum to 1 (bisection; the sum falls as lam rises)."""
    a, b = float((c - hi * d).min()) - 1, float((c - lo * d).max()) + 1
    for _ in range(200):
        lam = 0.5 * (a + b)
        if np.clip((c - lam) / d, lo, hi).sum() > 1:
            a = lam
        else:
            b = lam
    return np.clip((c - 0.5 * (a + b)) / d, lo, hi)


def test_the_gap_is_zero_at_the_optimum_and_bounds_the_suboptimality():
    rng = np.random.default_rng(7)
    for trial in range(30):
        n = int(rng.integers(3, 40))
        lo, hi = 0.0, max(0.3, 1.5 / n)
        d, c = rng.uniform(0.2, 3.0, n), rng.standard_normal(n)
        q = np.diag(d)
        star = _diagonal_optimum(d, c, lo, hi)
        b = Bounds(lo, hi)
        assert optimality_gap(star, q, c, b) < 1e-9, trial
        f_star = _f(q, c, star)
        for _ in range(10):  # any feasible point: 0 <= f - f* <= gap
            w = _project(star + rng.normal(scale=10 ** rng.uniform(-6, 0), size=n), lo, hi)
            gap = optimality_gap(w, q, c, b)
            assert -1e-12 <= _f(q, c, w) - f_star <= gap + 1e-12, trial


# --- the allocator's acceptance rule ----------------------------------------------------


def _fake_minimize(x: np.ndarray, status: int = 8, success: bool = False):
    message = "Positive directional derivative for linesearch"
    return lambda *a, **k: SimpleNamespace(x=x, status=status, success=success, message=message)


def test_a_failed_solve_at_a_point_that_is_not_optimal_is_still_refused(monkeypatch):
    n = 10
    rng = np.random.default_rng(1)
    a = rng.standard_normal((40, n))
    cov = a.T @ a / 40
    mu = rng.normal(0.05, 0.1, n)
    monkeypatch.setattr(allocators, "minimize", _fake_minimize(np.full(n, 1 / n)))
    with pytest.raises(OptimizationError, match=r"SLSQP failed: Positive.*optimality gap"):
        mean_variance(mu, cov, 5.0, Bounds(0, 0.4))
    # Infeasible or non-finite points are refused before any certificate is computed.
    for bad in (np.full(n, 0.2), np.full(n, np.nan)):
        monkeypatch.setattr(allocators, "minimize", _fake_minimize(bad))
        with pytest.raises(OptimizationError, match=r"^SLSQP failed: Positive[^(]*$"):
            mean_variance(mu, cov, 5.0, Bounds(0, 0.4))


def test_a_failed_solve_at_the_optimum_is_accepted(monkeypatch):
    n = 10
    d = np.linspace(0.5, 2.0, n)
    c = np.linspace(-0.2, 0.3, n)
    cov, ra = np.diag(d), 1.0
    q, cs = _scaled(cov, c, ra)
    star = _diagonal_optimum(np.diag(q).copy(), cs, 0.0, 0.4)
    monkeypatch.setattr(allocators, "minimize", _fake_minimize(star))
    w = mean_variance(c, cov, ra, Bounds(0, 0.4))
    np.testing.assert_allclose(w, np.where(star < allocators.DUST_WEIGHT, 0.0, star), atol=1e-15)


def test_a_successful_solve_is_returned_as_before_without_a_certificate(monkeypatch):
    # The success path is byte-for-byte the pre-#253 one: no gap is computed or required.
    def refuse(*a, **k):
        raise AssertionError("optimality_gap called on a successful solve")

    m = generate_market(SyntheticSpec(n_assets=30, start="2019-01-02", end="2021-12-31", seed=3))
    rets = (
        total_returns(m.bars, m.actions).drop(columns=[BENCHMARK_SYMBOL]).iloc[1:253].dropna(axis=1)
    )
    cov = covariance(rets, method="ledoit_wolf").to_numpy()
    before = min_variance(cov, Bounds(0, 0.4))
    monkeypatch.setattr(allocators, "optimality_gap", refuse)
    np.testing.assert_array_equal(min_variance(cov, Bounds(0, 0.4)), before)


# --- the issue's case: the optimize API's defaults at 100 symbols ------------------------


def _api_default_windows(seed: int, every: int):
    """The optimize API's inputs (routes: lookback 252, Ledoit-Wolf, risk aversion 5,
    mu = mean x 252, cap max(0.4, 1/n)) on a 100-asset synthetic market."""
    m = generate_market(
        SyntheticSpec(n_assets=100, start="2019-01-02", end="2021-12-31", seed=seed)
    )
    rets = total_returns(m.bars, m.actions).drop(columns=[BENCHMARK_SYMBOL])
    for end in range(253, len(rets) + 1, every):
        w = rets.iloc[end - 252 : end]
        w = w[[s for s in w.columns if w[s].notna().sum() >= int(0.9 * 252)]].dropna()
        yield covariance(w, method="ledoit_wolf").to_numpy(), w.mean().to_numpy() * 252


def test_mean_variance_at_the_api_defaults_with_100_symbols_is_certified_optimal(monkeypatch):
    statuses = []
    real = allocators.minimize

    def recording(*a, **k):
        res = real(*a, **k)
        statuses.append(int(res.status))
        return res

    monkeypatch.setattr(allocators, "minimize", recording)
    solved = 0
    for seed in (3, 11):
        for cov, mu in _api_default_windows(seed, every=160):
            n = cov.shape[0]
            cap = max(0.4, 1 / n)
            w = mean_variance(mu, cov, 5.0, Bounds(0, cap))  # raised before #253
            q, c = _scaled(0.5 * (cov + cov.T), mu, 5.0)
            ref = _reference(q, c, 0.0, cap)
            # Within float noise of the independent optimum, in objective and in weights.
            assert _f(q, c, w) - _f(q, c, ref) <= 1e-12
            assert np.abs(w - ref).max() <= 1e-6
            assert abs(w.sum() - 1) <= n * allocators.DUST_WEIGHT and w.max() <= cap
            solved += 1
    assert solved >= 8
    # The case is the issue's: most of these solves end in SLSQP mode 8 on both platforms.
    assert statuses.count(8) >= solved // 2, statuses
