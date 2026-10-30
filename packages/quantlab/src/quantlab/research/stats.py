"""Statistical uncertainty for backtest results.

* Stationary bootstrap confidence intervals (Politis & Romano, 1994).
* Probabilistic Sharpe ratio (Bailey & López de Prado, 2012).
* Deflated Sharpe ratio (Bailey & López de Prado, 2014), which adjusts the PSR benchmark
  for the number of strategy variants tried.

Sharpe ratios in the PSR/DSR formulas are *per period* (not annualized), as in the papers.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from quantlab.analytics.performance import ZERO_DISPERSION

EULER_GAMMA = 0.5772156649015329


def stationary_bootstrap_indices(
    n: int, n_samples: int, block: float, rng: np.random.Generator
) -> np.ndarray:
    p = 1.0 / block
    idx = np.empty((n_samples, n), dtype=np.int64)
    idx[:, 0] = rng.integers(0, n, n_samples)
    restart = rng.random((n_samples, n)) < p
    fresh = rng.integers(0, n, (n_samples, n))
    for t in range(1, n):
        idx[:, t] = np.where(restart[:, t], fresh[:, t], (idx[:, t - 1] + 1) % n)
    return idx


def _ann_sharpe(r: np.ndarray, periods: int, rf_p: float = 0.0) -> np.ndarray:
    sd = r.std(axis=-1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        s = (r.mean(axis=-1) - rf_p) / sd * math.sqrt(periods)
    return np.where(sd > ZERO_DISPERSION, s, np.nan)


def bootstrap_ci(
    returns: pd.Series,
    benchmark: pd.Series | None = None,
    *,
    n_samples: int = 1000,
    block: float = 10.0,
    confidence: float = 0.95,
    seed: int = 0,
    periods: int = 252,
    risk_free_rate: float = 0.0,
) -> dict[str, Any]:
    """Bootstrap CIs for annualized Sharpe, CAGR and (if given) annualized excess return.

    Strategy and benchmark returns are resampled with the same indices so their
    dependence is preserved.
    """
    r = returns.dropna().to_numpy(dtype=float)
    if len(r) < 60:
        return {"error": "need at least 60 observations for a bootstrap"}
    rng = np.random.default_rng(seed)
    idx = stationary_bootstrap_indices(len(r), n_samples, block, rng)
    sample = r[idx]
    lo, hi = (1 - confidence) / 2, 1 - (1 - confidence) / 2
    rf_p = (1 + risk_free_rate) ** (1 / periods) - 1  # same convention as compute_performance
    sharpe = _ann_sharpe(sample, periods, rf_p)
    finite = sharpe[np.isfinite(sharpe)]
    growth = np.prod(1 + sample, axis=1)
    cagr = np.where(growth > 0, growth ** (periods / len(r)) - 1, -1.0)
    estimate = float(_ann_sharpe(r, periods, rf_p))
    if math.isfinite(estimate) and len(finite):
        sharpe_ci: dict[str, Any] = {
            "estimate": estimate,
            "lower": float(np.quantile(finite, lo)),
            "upper": float(np.quantile(finite, hi)),
            "p_le_zero": float(np.mean(finite <= 0)),
        }
    else:
        sharpe_ci = {
            "estimate": None,
            "lower": None,
            "upper": None,
            "p_le_zero": None,
            "undefined": "zero variance of returns",
        }
    out: dict[str, Any] = {
        "method": "stationary_bootstrap",
        "samples": n_samples,
        "mean_block_length": block,
        "confidence": confidence,
        "seed": seed,
        "observations": len(r),
        "sharpe": sharpe_ci,
        "cagr": {"lower": float(np.quantile(cagr, lo)), "upper": float(np.quantile(cagr, hi))},
    }
    if benchmark is not None:
        b = benchmark.reindex(returns.dropna().index).to_numpy(dtype=float)
        if np.isfinite(b).all():
            active = (sample - b[idx]).mean(axis=1) * periods
            out["excess_return"] = {
                "estimate": float((r - b).mean() * periods),
                "lower": float(np.quantile(active, lo)),
                "upper": float(np.quantile(active, hi)),
                "p_le_zero": float(np.mean(active <= 0)),
            }
    return out


def probabilistic_sharpe(returns: pd.Series, benchmark_sr: float = 0.0) -> float | None:
    """P(true per-period Sharpe > ``benchmark_sr``) given sample length, skew and kurtosis."""
    r = returns.dropna().to_numpy(dtype=float)
    n = len(r)
    if n < 10 or r.std(ddof=1) <= ZERO_DISPERSION:
        return None
    sr = r.mean() / r.std(ddof=1)
    skew = float(stats.skew(r))
    kurt = float(stats.kurtosis(r, fisher=False))
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if denom <= 0:
        return None
    return float(stats.norm.cdf((sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(denom)))


def expected_max_sharpe(trial_sharpes: list[float]) -> float:
    """Expected maximum per-period Sharpe among N independent trials with zero true Sharpe."""
    n = len(trial_sharpes)
    if n < 2:
        return 0.0
    var = float(np.var(trial_sharpes, ddof=1))
    z1 = stats.norm.ppf(1 - 1 / n)
    z2 = stats.norm.ppf(1 - 1 / (n * math.e))
    return math.sqrt(var) * ((1 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def deflated_sharpe(returns: pd.Series, trial_sharpes: list[float]) -> dict[str, Any]:
    """DSR: the PSR against the Sharpe one would expect from the best of N null trials.

    ``trial_sharpes`` are per-period Sharpe ratios of every variant evaluated on the same
    data (including the selected one).
    """
    sr0 = expected_max_sharpe(trial_sharpes)
    return {
        "trials": len(trial_sharpes),
        "expected_max_sharpe_per_period": sr0,
        "psr_vs_zero": probabilistic_sharpe(returns, 0.0),
        "deflated_sharpe": probabilistic_sharpe(returns, sr0),
    }


def regime_analysis(
    returns: pd.Series,
    benchmark_returns: pd.Series,
    *,
    vol_window: int = 63,
    trend_window: int = 126,
    periods: int = 252,
) -> list[dict[str, Any]]:
    """Performance by market regime, regimes defined from trailing benchmark data only."""
    b = benchmark_returns.reindex(returns.index)
    vol = b.rolling(vol_window).std(ddof=1)
    trend = (1 + b).rolling(trend_window).apply(np.prod, raw=True) - 1
    frame = pd.DataFrame({"r": returns, "b": b, "vol": vol.shift(1), "trend": trend.shift(1)})
    frame = frame.dropna()
    if len(frame) < 60:
        return []
    terciles = frame["vol"].quantile([1 / 3, 2 / 3]).to_numpy()
    frame["vol_regime"] = np.select(
        [frame["vol"] <= terciles[0], frame["vol"] <= terciles[1]],
        ["low vol", "mid vol"],
        "high vol",
    )
    frame["trend_regime"] = np.where(frame["trend"] >= 0, "up trend", "down trend")
    rows = []
    for col in ("vol_regime", "trend_regime"):
        for name, g in frame.groupby(col, sort=True):

            def ann(x: pd.Series) -> tuple[float, float | None]:
                sd = float(x.std(ddof=1))
                mean = float(x.mean()) * periods
                return mean, (float(x.mean()) / sd * math.sqrt(periods) if sd > 0 else None)

            sm, ss = ann(g["r"])
            bm, bs = ann(g["b"])
            rows.append(
                {
                    "dimension": col.replace("_regime", ""),
                    "regime": str(name),
                    "sessions": len(g),
                    "strategy_return": sm,
                    "strategy_sharpe": ss,
                    "benchmark_return": bm,
                    "benchmark_sharpe": bs,
                }
            )
    return rows
