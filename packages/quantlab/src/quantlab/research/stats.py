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


def _excess(returns: pd.Series, risk_free_rate: float, periods: int) -> np.ndarray:
    rf_p = (1 + risk_free_rate) ** (1 / periods) - 1  # same convention as compute_performance
    return returns.dropna().to_numpy(dtype=float) - rf_p


def per_period_sharpe(
    returns: pd.Series, *, risk_free_rate: float = 0.0, periods: int = 252
) -> float | None:
    """Non-annualized Sharpe of excess returns; ``None`` without dispersion.

    This is the statistic the deflated Sharpe ratio compares across trials, and equals
    ``compute_performance(...)["sharpe"] / sqrt(periods)`` for the same returns.
    """
    ex = _excess(returns, risk_free_rate, periods)
    if len(ex) < 2:
        return None
    sd = float(ex.std(ddof=1))
    return None if sd <= ZERO_DISPERSION else float(ex.mean() / sd)


def probabilistic_sharpe(
    returns: pd.Series,
    benchmark_sr: float = 0.0,
    *,
    risk_free_rate: float = 0.0,
    periods: int = 252,
) -> float | None:
    """P(true per-period excess Sharpe > ``benchmark_sr``) given length, skew and kurtosis.

    Excess returns are over the per-period risk-free rate; skew and kurtosis do not depend
    on it.
    """
    r = _excess(returns, risk_free_rate, periods)
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


def expected_max_sharpe(trial_sharpes: list[float], n_trials: int | None = None) -> float:
    """Expected maximum per-period Sharpe among N independent trials with zero true Sharpe.

    ``n_trials`` is the number of configurations tried. It can exceed the number of
    Sharpes when some trials have none (no dispersion): those trials were still tried, so
    they count toward N, while the variance comes from the defined Sharpes.
    """
    n = max(len(trial_sharpes), n_trials or 0)
    if len(trial_sharpes) < 2:
        return 0.0
    var = float(np.var(trial_sharpes, ddof=1))
    z1 = stats.norm.ppf(1 - 1 / n)
    z2 = stats.norm.ppf(1 - 1 / (n * math.e))
    return math.sqrt(var) * ((1 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def deflated_sharpe(
    returns: pd.Series,
    trial_sharpes: list[float],
    *,
    risk_free_rate: float = 0.0,
    periods: int = 252,
    n_trials: int | None = None,
) -> dict[str, Any]:
    """DSR: the PSR against the Sharpe one would expect from the best of N null trials.

    ``n_trials`` counts every configuration tried, including those without a defined
    Sharpe, so N is not understated (which would make the DSR less conservative).

    ``trial_sharpes`` are :func:`per_period_sharpe` values of every variant evaluated on the
    same data (including the selected one). ``returns`` must be the selected variant's
    returns over the same window, with the same ``risk_free_rate``, that produced its
    trial Sharpe; otherwise the comparison with the null maximum is biased.
    """
    sr0 = expected_max_sharpe(trial_sharpes, n_trials)
    rf, p = risk_free_rate, periods
    return {
        "trials": max(len(trial_sharpes), n_trials or 0),
        "trials_with_sharpe": len(trial_sharpes),
        "observations": int(returns.notna().sum()),
        "selected_sharpe_per_period": per_period_sharpe(returns, risk_free_rate=rf, periods=p),
        "expected_max_sharpe_per_period": sr0,
        "psr_vs_zero": probabilistic_sharpe(returns, 0.0, risk_free_rate=rf, periods=p),
        "deflated_sharpe": probabilistic_sharpe(returns, sr0, risk_free_rate=rf, periods=p),
    }


def regime_analysis(
    returns: pd.Series,
    benchmark_returns: pd.Series,
    *,
    vol_window: int = 63,
    trend_window: int = 126,
    periods: int = 252,
) -> list[dict[str, Any]]:
    """Performance by market regime (ex-post diagnostics).

    Each day's volatility and trend come from trailing benchmark data, but the volatility
    tercile cut-offs are computed over the whole sample, so the labels are not something a
    strategy could have known in advance; never feed them back into a strategy.
    """
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
