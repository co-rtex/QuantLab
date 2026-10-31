"""Portfolio risk analytics.

All loss measures (VaR, expected shortfall) are reported as *positive* fractions of
portfolio value over a one-period horizon unless stated otherwise.

Limitations, documented for users:
* Historical VaR/ES assume the sample is representative of the future; they cannot
  exceed the worst loss in the sample and are noisy for small samples or high confidence.
* Gaussian VaR ignores fat tails and skew; Cornish-Fisher partly corrects for skew and
  kurtosis but can misbehave for extreme moments.
* Square-root-of-time scaling assumes i.i.d. returns.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from quantlab.analytics.performance import PERIODS_PER_YEAR, ZERO_DISPERSION
from quantlab.optimize.allocators import risk_contributions


class RiskError(ValueError):
    pass


def _clean(returns: pd.Series | np.ndarray, min_obs: int = 20) -> np.ndarray:
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < min_obs:
        raise RiskError(f"need at least {min_obs} observations, got {len(r)}")
    return r


def historical_var(returns: pd.Series | np.ndarray, confidence: float = 0.95) -> float:
    if not 0.5 < confidence < 1:
        raise RiskError("confidence must be in (0.5, 1)")
    r = _clean(returns)
    return float(-np.quantile(r, 1 - confidence, method="lower"))


def historical_es(returns: pd.Series | np.ndarray, confidence: float = 0.95) -> float:
    r = _clean(returns)
    q = np.quantile(r, 1 - confidence, method="lower")
    tail = r[r <= q]
    return float(-tail.mean())


def gaussian_var(returns: pd.Series | np.ndarray, confidence: float = 0.95) -> float:
    r = _clean(returns)
    z = stats.norm.ppf(1 - confidence)
    return float(-(r.mean() + z * r.std(ddof=1)))


def gaussian_es(returns: pd.Series | np.ndarray, confidence: float = 0.95) -> float:
    r = _clean(returns)
    a = 1 - confidence
    return float(-(r.mean() - r.std(ddof=1) * stats.norm.pdf(stats.norm.ppf(a)) / a))


def cornish_fisher_var(returns: pd.Series | np.ndarray, confidence: float = 0.95) -> float:
    r = _clean(returns)
    if r.std(ddof=1) <= ZERO_DISPERSION:
        # Skew and kurtosis are undefined, but the expansion's limit is the certain outcome.
        return float(-r.mean())
    z = stats.norm.ppf(1 - confidence)
    s = stats.skew(r)
    k = stats.kurtosis(r)  # excess
    zcf = z + (z**2 - 1) * s / 6 + (z**3 - 3 * z) * k / 24 - (2 * z**3 - 5 * z) * s**2 / 36
    return float(-(r.mean() + zcf * r.std(ddof=1)))


def covariance(returns: pd.DataFrame, method: str = "sample", annualize: int = 252) -> pd.DataFrame:
    r = returns.dropna(how="any")
    if len(r) < 2:
        raise RiskError("not enough overlapping observations for covariance")
    if method == "sample":
        cov = r.cov(ddof=1).to_numpy()
    elif method == "ledoit_wolf":
        from sklearn.covariance import LedoitWolf

        cov = LedoitWolf().fit(r.to_numpy()).covariance_
    else:
        raise RiskError(f"unknown covariance method {method}")
    return pd.DataFrame(cov * annualize, index=r.columns, columns=r.columns)


def correlation(returns: pd.DataFrame) -> pd.DataFrame:
    return returns.dropna(how="any").corr()


def concentration(weights: pd.Series) -> dict[str, float | None]:
    w = weights[weights.abs() > 0].abs()
    total = float(w.sum())
    if total == 0:
        return {"hhi": None, "effective_n": None, "top1": None, "top5": None, "top10": None}
    w = w / total
    hhi = float((w**2).sum())
    s = w.sort_values(ascending=False)
    return {
        "hhi": hhi,
        "effective_n": 1.0 / hhi,
        "top1": float(s.iloc[:1].sum()),
        "top5": float(s.iloc[:5].sum()),
        "top10": float(s.iloc[:10].sum()),
    }


def risk_decomposition(weights: pd.Series, asset_returns: pd.DataFrame) -> pd.DataFrame:
    syms = [s for s in weights.index if s in asset_returns.columns]
    if not syms:
        raise RiskError("no overlap between weights and returns")
    cov = covariance(asset_returns[syms])
    w = weights[syms].to_numpy(dtype=float)
    out = risk_contributions(w, cov.to_numpy())
    out.index = syms
    return out.sort_values("component", ascending=False)


@dataclass(frozen=True)
class StressScenario:
    name: str
    shocks: dict[str, float]  # symbol or sector -> return shock
    market_shock: float | None = None
    description: str = ""


def apply_stress(
    weights: pd.Series,
    scenario: StressScenario,
    betas: pd.Series | None = None,
    sectors: pd.Series | None = None,
) -> dict[str, Any]:
    """Instantaneous P&L of a scenario: explicit symbol shocks, then sector shocks, then a
    beta-scaled market shock for anything not otherwise specified."""
    contrib = {}
    for sym, w in weights.items():
        shock = None
        if sym in scenario.shocks:
            shock = scenario.shocks[sym]
        elif sectors is not None and sectors.get(sym) in scenario.shocks:
            shock = scenario.shocks[sectors[sym]]
        elif scenario.market_shock is not None:
            beta = 1.0 if betas is None else float(betas.get(sym, 1.0))
            shock = beta * scenario.market_shock
        contrib[sym] = float(w) * (shock or 0.0)
    pnl = sum(contrib.values())
    return {
        "scenario": scenario.name,
        "portfolio_return": pnl,
        "contributions": dict(sorted(contrib.items(), key=lambda kv: kv[1])),
    }


def worst_periods(equity: pd.Series, window: int = 21, top: int = 5) -> pd.DataFrame:
    """Historical stress windows: the worst non-overlapping rolling-window returns."""
    roll = equity / equity.shift(window) - 1
    roll = roll.dropna().sort_values()
    picked: list[pd.Timestamp] = []
    rows = []
    pos = {d: i for i, d in enumerate(equity.index)}
    for d, v in roll.items():
        if all(abs(pos[d] - pos[p]) >= window for p in picked):
            picked.append(d)
            rows.append({"start": equity.index[pos[d] - window], "end": d, "return": float(v)})
        if len(rows) >= top:
            break
    return pd.DataFrame(rows, columns=["start", "end", "return"])


def monte_carlo(
    returns: pd.Series | np.ndarray,
    horizon: int = 252,
    n_paths: int = 2000,
    block: int = 10,
    seed: int = 0,
    initial: float = 1.0,
) -> dict[str, Any]:
    """Stationary-bootstrap simulation of terminal wealth (Politis & Romano, 1994).

    Resampling blocks of mean length ``block`` preserves short-range dependence such as
    volatility clustering better than i.i.d. resampling.
    """
    r = _clean(returns, min_obs=max(30, block))
    rng = np.random.default_rng(seed)
    n = len(r)
    p = 1.0 / block
    idx = np.empty((n_paths, horizon), dtype=np.int64)
    idx[:, 0] = rng.integers(0, n, n_paths)
    restart = rng.random((n_paths, horizon)) < p
    fresh = rng.integers(0, n, (n_paths, horizon))
    for t in range(1, horizon):
        idx[:, t] = np.where(restart[:, t], fresh[:, t], (idx[:, t - 1] + 1) % n)
    paths = initial * np.cumprod(1 + r[idx], axis=1)
    terminal = paths[:, -1]
    running_max = np.maximum.accumulate(np.c_[np.full(n_paths, initial), paths], axis=1)
    mdd = (np.c_[np.full(n_paths, initial), paths] / running_max - 1).min(axis=1)
    qs = [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]
    fan = np.quantile(paths, [0.05, 0.25, 0.5, 0.75, 0.95], axis=0)
    return {
        "method": "stationary_bootstrap",
        "horizon": horizon,
        "paths": n_paths,
        "mean_block_length": block,
        "seed": seed,
        "terminal_quantiles": {str(q): float(np.quantile(terminal, q)) for q in qs},
        "probability_of_loss": float((terminal < initial).mean()),
        "expected_terminal": float(terminal.mean()),
        "max_drawdown_quantiles": {str(q): float(np.quantile(mdd, q)) for q in (0.05, 0.5, 0.95)},
        "fan": {
            "p05": fan[0].tolist(),
            "p25": fan[1].tolist(),
            "p50": fan[2].tolist(),
            "p75": fan[3].tolist(),
            "p95": fan[4].tolist(),
        },
    }


def risk_summary(
    returns: pd.Series, confidence: float = 0.95, *, periods_per_year: int = PERIODS_PER_YEAR
) -> dict[str, Any]:
    """One-period VaR/ES and volatility. These are loss and dispersion measures of raw
    returns, so the risk-free rate does not enter (it shifts the mean, not the spread)."""
    r = returns.dropna()
    out: dict[str, Any] = {
        "confidence": confidence,
        "observations": len(r),
        "periods_per_year": periods_per_year,
    }
    try:
        out.update(
            historical_var=historical_var(r, confidence),
            historical_es=historical_es(r, confidence),
            gaussian_var=gaussian_var(r, confidence),
            gaussian_es=gaussian_es(r, confidence),
            cornish_fisher_var=cornish_fisher_var(r, confidence),
            daily_volatility=float(r.std(ddof=1)),
            annualized_volatility=float(r.std(ddof=1) * math.sqrt(periods_per_year)),
        )
    except RiskError as exc:
        out["error"] = str(exc)
    return out
