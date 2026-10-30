"""Performance statistics with explicit treatment of undefined values.

Conventions (documented in docs/METHODOLOGY.md):

* Input is a daily equity (wealth) series sampled on trading sessions.
* Simple returns ``r_t = E_t / E_{t-1} - 1``; ``periods_per_year`` defaults to 252.
* The annual risk-free rate ``rf`` is converted to a per-period rate geometrically:
  ``rf_p = (1 + rf) ** (1 / periods_per_year) - 1``.
* Volatility uses the sample standard deviation (ddof=1).
* Sharpe = mean(r - rf_p) / std(r - rf_p) * sqrt(P).
* Sortino = mean(r - rf_p) / DD * sqrt(P) with DD = sqrt(mean(min(r - rf_p, 0)^2)).
* CAGR = (E_T / E_0) ** (P / N) - 1 where N is the number of return periods.
* Beta = Cov(r_p, r_b) / Var(r_b); Jensen's alpha (annualized, arithmetic) =
  P * [mean(r_p - rf_p) - beta * mean(r_b - rf_p)].
* Tracking error = std(r_p - r_b) * sqrt(P); information ratio = P * mean(r_p - r_b) / TE.
* Calmar = CAGR / |max drawdown|.

Any statistic that is mathematically undefined for the input (zero variance, too few
observations, non-positive wealth) is reported as ``None`` together with a reason in
``undefined`` instead of a misleading number such as 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

PERIODS_PER_YEAR = 252


@dataclass
class MetricSet:
    values: dict[str, float | int | None] = field(default_factory=dict)
    undefined: dict[str, str] = field(default_factory=dict)

    def set(self, name: str, value: float | int | None, reason: str | None = None) -> None:
        if value is None or (isinstance(value, float) and not math.isfinite(value)):
            self.values[name] = None
            self.undefined[name] = reason or "not finite"
        else:
            self.values[name] = float(value) if isinstance(value, (float, np.floating)) else value

    def __getitem__(self, name: str) -> float | int | None:
        return self.values[name]

    def to_dict(self) -> dict[str, Any]:
        return {"values": dict(self.values), "undefined": dict(self.undefined)}


def simple_returns(equity: pd.Series) -> pd.Series:
    e = equity.astype(float)
    return (e / e.shift(1) - 1.0).iloc[1:]


def log_returns(equity: pd.Series) -> pd.Series:
    e = equity.astype(float)
    if (e <= 0).any():
        raise ValueError("log returns undefined for non-positive wealth")
    return np.log(e / e.shift(1)).iloc[1:]


def drawdown_series(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return equity / peak - 1.0


def drawdown_episodes(equity: pd.Series, top: int | None = None) -> pd.DataFrame:
    """Peak-to-trough-to-recovery episodes sorted by depth.

    Durations are counted in sessions. ``recovery`` is ``NaT`` (and ``recovery_sessions``
    is ``None``) when the equity never regains the prior peak within the sample.
    """
    dd = drawdown_series(equity)
    rows: list[tuple[int, int, int | None]] = []
    in_dd = False
    peak_i = 0
    idx = equity.index
    vals = dd.to_numpy()
    for i, v in enumerate(vals):
        if v < 0 and not in_dd:
            in_dd, peak_i = True, i - 1
        elif v >= 0 and in_dd:
            seg = vals[peak_i : i + 1]
            trough_i = peak_i + int(np.argmin(seg))
            rows.append((peak_i, trough_i, i))
            in_dd = False
    if in_dd:
        seg = vals[peak_i:]
        rows.append((peak_i, peak_i + int(np.argmin(seg)), None))
    out = pd.DataFrame(
        [
            {
                "peak": idx[p],
                "trough": idx[t],
                "recovery": idx[r] if r is not None else pd.NaT,
                "depth": float(vals[t]),
                "decline_sessions": t - p,
                "recovery_sessions": (r - t) if r is not None else None,
                "total_sessions": (r - p) if r is not None else len(vals) - 1 - p,
            }
            for p, t, r in rows
        ],
        columns=[
            "peak",
            "trough",
            "recovery",
            "depth",
            "decline_sessions",
            "recovery_sessions",
            "total_sessions",
        ],
    )
    out = out.sort_values("depth", kind="stable").reset_index(drop=True)
    return out.head(top) if top else out


def compute_performance(
    equity: pd.Series,
    benchmark: pd.Series | None = None,
    *,
    risk_free_rate: float = 0.0,
    periods_per_year: int = PERIODS_PER_YEAR,
) -> MetricSet:
    m = MetricSet()
    e = equity.dropna().astype(float)
    P = periods_per_year
    m.set("observations", len(e))
    if len(e) < 2:
        for k in (
            "total_return",
            "cagr",
            "volatility",
            "sharpe",
            "sortino",
            "max_drawdown",
            "calmar",
        ):
            m.set(k, None, "fewer than two observations")
        return m
    if e.iloc[0] <= 0:
        m.set("total_return", None, "initial wealth is not positive")
        return m
    r = simple_returns(e)
    n = len(r)
    rf_p = (1 + risk_free_rate) ** (1 / P) - 1
    ex = r - rf_p
    m.set("total_return", e.iloc[-1] / e.iloc[0] - 1)
    ratio = e.iloc[-1] / e.iloc[0]
    if ratio > 0:
        m.set("cagr", ratio ** (P / n) - 1)
    elif ratio == 0:
        m.set("cagr", -1.0)
    else:
        m.set("cagr", None, "terminal wealth is negative")
    m.set("annualized_mean_return", r.mean() * P)
    if (e > 0).all():
        m.set("annualized_log_return", float(np.log(e.iloc[-1] / e.iloc[0])) * P / n)
    else:
        m.set("annualized_log_return", None, "non-positive wealth in sample")

    if n < 2:
        m.set("volatility", None, "fewer than two returns")
        m.set("sharpe", None, "fewer than two returns")
    else:
        sd = float(r.std(ddof=1))
        m.set("volatility", sd * math.sqrt(P))
        sd_ex = float(ex.std(ddof=1))
        if sd_ex <= 1e-15:
            m.set("sharpe", None, "zero volatility of excess returns")
        else:
            m.set("sharpe", ex.mean() / sd_ex * math.sqrt(P))
    downside = float(np.sqrt(np.mean(np.minimum(ex.to_numpy(), 0.0) ** 2)))
    if downside <= 1e-15:
        m.set("sortino", None, "no returns below the risk-free target")
    else:
        m.set("sortino", ex.mean() / downside * math.sqrt(P))

    dd = drawdown_series(e)
    mdd = float(dd.min())
    m.set("max_drawdown", mdd)
    eps = drawdown_episodes(e)
    if len(eps):
        worst = eps.iloc[0]
        m.set("max_drawdown_sessions", int(worst["total_sessions"]))
        rec = worst["recovery_sessions"]
        if rec is None or (isinstance(rec, float) and math.isnan(rec)):
            m.set("max_drawdown_recovery_sessions", None, "not recovered within sample")
        else:
            m.set("max_drawdown_recovery_sessions", int(rec))
        m.set("longest_drawdown_sessions", int(eps["total_sessions"].max()))
    else:
        m.set("max_drawdown_sessions", 0)
        m.set("max_drawdown_recovery_sessions", 0)
        m.set("longest_drawdown_sessions", 0)
    cagr = m.values.get("cagr")
    if mdd == 0:
        m.set("calmar", None, "no drawdown in sample")
    elif cagr is None:
        m.set("calmar", None, "CAGR undefined")
    else:
        m.set("calmar", float(cagr) / abs(mdd))
    m.set("best_day", r.max())
    m.set("worst_day", r.min())
    m.set("hit_rate", float((r > 0).mean()))
    m.set("skewness", float(r.skew()) if n > 2 else None, "fewer than three returns")
    m.set("excess_kurtosis", float(r.kurt()) if n > 3 else None, "fewer than four returns")

    if benchmark is not None:
        b = benchmark.reindex(e.index).astype(float)
        if b.isna().any() or (b <= 0).any():
            for k in (
                "beta",
                "alpha",
                "tracking_error",
                "information_ratio",
                "correlation",
                "benchmark_total_return",
                "excess_total_return",
            ):
                m.set(k, None, "benchmark missing or non-positive on some dates")
        else:
            rb = simple_returns(b)
            active = r - rb
            var_b = float(rb.var(ddof=1))
            m.set("benchmark_total_return", b.iloc[-1] / b.iloc[0] - 1)
            m.set("excess_total_return", (e.iloc[-1] / e.iloc[0]) - (b.iloc[-1] / b.iloc[0]))
            if var_b <= 1e-18:
                m.set("beta", None, "benchmark has zero variance")
                m.set("alpha", None, "beta undefined")
                m.set("correlation", None, "benchmark has zero variance")
            else:
                beta = float(np.cov(r, rb, ddof=1)[0, 1] / var_b)
                m.set("beta", beta)
                m.set("alpha", P * (ex.mean() - beta * (rb - rf_p).mean()))
                sd_r = float(r.std(ddof=1))
                m.set(
                    "correlation",
                    float(np.corrcoef(r, rb)[0, 1]) if sd_r > 0 else None,
                    "portfolio has zero variance",
                )
            te = float(active.std(ddof=1)) * math.sqrt(P)
            if te <= 1e-15:
                m.set("tracking_error", 0.0)
                m.set("information_ratio", None, "zero tracking error")
            else:
                m.set("tracking_error", te)
                m.set("information_ratio", active.mean() * P / te)
    return m


def trade_statistics(trades: pd.DataFrame) -> MetricSet:
    """Round-trip statistics using FIFO lot matching on executed trades.

    A round trip is a sell matched against earlier buy lots; realized P&L includes
    commissions allocated pro rata to the matched quantity.
    """
    m = MetricSet()
    if trades is None or len(trades) == 0:
        m.set("round_trips", 0)
        for k in ("win_rate", "avg_win", "avg_loss", "profit_factor"):
            m.set(k, None, "no closed round trips")
        return m
    lots: dict[str, list[list[float]]] = {}
    pnls: list[float] = []
    for t in trades.sort_values(["date"], kind="stable").itertuples():
        q, px = float(t.quantity), float(t.price)
        fee_ps = float(t.commission) / abs(q) if q else 0.0
        if q > 0:
            lots.setdefault(t.symbol, []).append([q, px + fee_ps])
            continue
        remaining = -q
        book = lots.get(t.symbol, [])
        pnl = 0.0
        while remaining > 1e-12 and book:
            lot = book[0]
            take = min(lot[0], remaining)
            pnl += take * ((px - fee_ps) - lot[1])
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-12:
                book.pop(0)
        pnls.append(pnl)
    arr = np.array(pnls)
    m.set("round_trips", len(arr))
    if len(arr) == 0:
        for k in ("win_rate", "avg_win", "avg_loss", "profit_factor"):
            m.set(k, None, "no closed round trips")
        return m
    wins, losses = arr[arr > 0], arr[arr < 0]
    m.set("win_rate", float(len(wins) / len(arr)))
    m.set("avg_win", float(wins.mean()) if len(wins) else None, "no winning trades")
    m.set("avg_loss", float(losses.mean()) if len(losses) else None, "no losing trades")
    m.set(
        "profit_factor",
        float(wins.sum() / -losses.sum()) if len(losses) else None,
        "no losing trades",
    )
    return m


def cost_and_turnover(
    equity_frame: pd.DataFrame, periods_per_year: int = PERIODS_PER_YEAR
) -> MetricSet:
    """Turnover and transaction-cost attribution from the engine's daily records.

    Annualized turnover is one-way: sum(traded notional) / 2 / mean(equity), annualized.
    Cost drag is the difference between gross (cost-free approximation) and net CAGR.
    """
    m = MetricSet()
    eq = equity_frame
    n = max(len(eq) - 1, 1)
    years = n / periods_per_year
    avg_eq = float(eq["equity"].mean())
    traded = float(eq["traded_notional"].sum())
    m.set("total_traded_notional", traded)
    m.set(
        "annual_turnover",
        traded / 2 / avg_eq / years if avg_eq > 0 else None,
        "non-positive average equity",
    )
    comm = float(eq["cum_commission"].iloc[-1])
    slip = float(eq["cum_slippage"].iloc[-1])
    m.set("total_commission", comm)
    m.set("total_slippage", slip)
    m.set("total_costs", comm + slip)
    m.set("total_dividends", float(eq["cum_dividends"].iloc[-1]))
    initial = float(eq["equity"].iloc[0])
    m.set("costs_pct_initial_capital", (comm + slip) / initial)
    net = compute_performance(eq["equity"], periods_per_year=periods_per_year)
    gross = compute_performance(eq["gross_equity"], periods_per_year=periods_per_year)
    m.set("gross_cagr", gross.values.get("cagr"), "gross CAGR undefined")
    m.set("net_cagr", net.values.get("cagr"), "net CAGR undefined")
    if net.values.get("cagr") is not None and gross.values.get("cagr") is not None:
        m.set("cost_drag_cagr", float(gross["cagr"]) - float(net["cagr"]))  # type: ignore[arg-type]
    else:
        m.set("cost_drag_cagr", None, "CAGR undefined")
    return m
