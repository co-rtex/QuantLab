"""Long-only portfolio allocators with validated outputs.

Every allocator returns weights that sum to 1 within ``tol`` and respect the bounds, or
raises ``OptimizationError``. Solver output is never returned unchecked. The SLSQP
allocators also set solver residue at a lower bound of 0 to exactly 0 (``drop_dust``).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import minimize


class OptimizationError(RuntimeError):
    pass


class InfeasibleConstraints(OptimizationError):
    pass


@dataclass(frozen=True)
class Bounds:
    lower: float = 0.0
    upper: float = 1.0

    def check(self, n: int) -> None:
        if not (0.0 <= self.lower <= self.upper <= 1.0):
            raise InfeasibleConstraints(f"invalid bounds [{self.lower}, {self.upper}]")
        if n == 0:
            raise InfeasibleConstraints("no assets")
        if n * self.lower > 1 + 1e-12 or n * self.upper < 1 - 1e-12:
            raise InfeasibleConstraints(
                f"bounds [{self.lower}, {self.upper}] cannot sum to 1 across {n} assets"
            )


def validate_weights(w: np.ndarray, bounds: Bounds, tol: float = 1e-6) -> np.ndarray:
    if not np.all(np.isfinite(w)):
        raise OptimizationError("solver returned non-finite weights")
    if abs(w.sum() - 1.0) > tol:
        raise OptimizationError(f"weights sum to {w.sum():.8f}, not 1")
    if (w < bounds.lower - tol).any() or (w > bounds.upper + tol).any():
        raise OptimizationError("weights violate bounds")
    w = np.clip(w, bounds.lower, bounds.upper)
    return w / w.sum()


def _check_cov(cov: np.ndarray) -> np.ndarray:
    cov = np.asarray(cov, dtype=float)
    if cov.ndim != 2 or cov.shape[0] != cov.shape[1]:
        raise OptimizationError("covariance must be square")
    if not np.all(np.isfinite(cov)):
        raise OptimizationError("covariance contains non-finite values")
    cov = 0.5 * (cov + cov.T)
    eig = np.linalg.eigvalsh(cov)
    if eig.min() < -1e-10 * max(1.0, eig.max()):
        raise OptimizationError("covariance is not positive semi-definite")
    return cov


def equal_weight(n: int) -> np.ndarray:
    if n <= 0:
        raise InfeasibleConstraints("no assets")
    return np.full(n, 1.0 / n)


def _project_capped_simplex(v: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Euclidean projection onto {w : sum w = 1, lo <= w <= hi} by bisection on the shift."""
    a, b = v.min() - hi - 1, v.max() - lo + 1
    for _ in range(200):
        tau = 0.5 * (a + b)
        if np.clip(v - tau, lo, hi).sum() > 1:
            a = tau
        else:
            b = tau
    return np.clip(v - 0.5 * (a + b), lo, hi)


def _solve_qp(
    cov: np.ndarray, mu: np.ndarray | None, risk_aversion: float, bounds: Bounds
) -> np.ndarray:
    n = cov.shape[0]
    bounds.check(n)
    lin = np.zeros(n) if mu is None else np.asarray(mu, dtype=float)
    # Rescale the objective to O(1); a positive constant factor leaves the argmin unchanged
    # but keeps SLSQP's absolute tolerances meaningful for small variances.
    scale = max(float(np.trace(cov)) / n * risk_aversion, float(np.abs(lin).max()), 1e-12)
    q, c = risk_aversion * cov / scale, lin / scale

    def obj(w: np.ndarray) -> float:
        return float(0.5 * w @ q @ w - c @ w)

    def grad(w: np.ndarray) -> np.ndarray:
        return q @ w - c

    x0 = _project_capped_simplex(np.full(n, 1.0 / n), bounds.lower, bounds.upper)
    res = minimize(
        obj,
        x0,
        jac=grad,
        method="SLSQP",
        bounds=[(bounds.lower, bounds.upper)] * n,
        constraints=[{"type": "eq", "fun": lambda w: w.sum() - 1.0, "jac": lambda w: np.ones(n)}],
        options={"maxiter": 500, "ftol": 1e-15},
    )
    if not res.success:
        raise OptimizationError(f"SLSQP failed: {res.message}")
    return drop_dust(validate_weights(res.x, bounds), bounds)


# SLSQP leaves weights at an active lower bound of 0 at about 1e-20 to 1e-14 instead of 0
# (#227). On markets 3, 11 and 29 the largest such residue was 8.1e-15 and the smallest
# real weight 2.3e-6, in both min_variance and mean_variance; nothing lay between. A
# residue became an order from zero holdings in a large account, paying commission_min.
# The threshold sits about four decades from each side. It depends on SLSQP's ftol and on
# the O(1) objective scaling above: change either and measure again
# (tests/unit/test_allocator_dust.py).
DUST_WEIGHT = 1e-10


def drop_dust(w: np.ndarray, bounds: Bounds) -> np.ndarray:
    """Set weights under ``DUST_WEIGHT`` to 0 when 0 is allowed, without renormalizing: the
    other weights keep their exact values, and the dropped mass (under n * DUST_WEIGHT)
    stays in cash."""
    if bounds.lower > 0:
        return w
    return np.where(w < DUST_WEIGHT, 0.0, w)


def min_variance(cov: np.ndarray, bounds: Bounds | None = None) -> np.ndarray:
    return _solve_qp(_check_cov(cov), None, 1.0, bounds or Bounds())


def mean_variance(
    mu: np.ndarray, cov: np.ndarray, risk_aversion: float = 5.0, bounds: Bounds | None = None
) -> np.ndarray:
    """Maximize ``mu'w - (risk_aversion/2) w'Σw`` subject to full investment and bounds."""
    if risk_aversion <= 0:
        raise OptimizationError("risk aversion must be positive")
    mu = np.asarray(mu, dtype=float)
    if not np.all(np.isfinite(mu)):
        raise OptimizationError("expected returns contain non-finite values")
    return _solve_qp(_check_cov(cov), mu, risk_aversion, bounds or Bounds())


def risk_parity(cov: np.ndarray, budget: np.ndarray | None = None) -> np.ndarray:
    """Risk budgeting via the convex formulation of Spinu (2013).

    Minimize ``0.5 y'Σy - Σ b_i log y_i`` over y > 0; then ``w = y / sum(y)`` has risk
    contributions proportional to ``b``.
    """
    cov = _check_cov(cov)
    n = cov.shape[0]
    b = np.full(n, 1.0 / n) if budget is None else np.asarray(budget, dtype=float)
    if b.shape != (n,) or (b <= 0).any():
        raise OptimizationError("risk budget must be positive with one entry per asset")
    b = b / b.sum()
    if np.any(np.diag(cov) <= 0):
        raise OptimizationError("risk parity requires positive variances")
    vol = np.sqrt(np.diag(cov))
    y0 = 1.0 / vol

    def obj(y: np.ndarray) -> float:
        return float(0.5 * y @ cov @ y - b @ np.log(y))

    def grad(y: np.ndarray) -> np.ndarray:
        return cov @ y - b / y

    res = minimize(
        obj,
        y0 / y0.sum(),
        jac=grad,
        method="L-BFGS-B",
        bounds=[(1e-12, None)] * n,
        options={"maxiter": 5000, "gtol": 1e-12},
    )
    if not res.success and "ABNORMAL" not in str(res.message):
        raise OptimizationError(f"risk parity failed: {res.message}")
    w = res.x / res.x.sum()
    rc = w * (cov @ w)
    rc = rc / rc.sum()
    if np.max(np.abs(rc - b)) > 1e-4:
        raise OptimizationError("risk parity did not converge to target budget")
    return validate_weights(w, Bounds())


def risk_contributions(w: np.ndarray, cov: np.ndarray) -> pd.DataFrame:
    w = np.asarray(w, dtype=float)
    sigma = float(np.sqrt(w @ cov @ w))
    if sigma == 0:
        raise OptimizationError("zero portfolio volatility; contributions undefined")
    mrc = cov @ w / sigma
    crc = w * mrc
    return pd.DataFrame({"weight": w, "marginal": mrc, "component": crc, "pct": crc / sigma})
