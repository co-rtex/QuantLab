"""Turn a ``BacktestResult`` into a JSON-safe analytics document."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from quantlab.analytics.performance import (
    compute_performance,
    cost_and_turnover,
    drawdown_episodes,
    simple_returns,
    trade_statistics,
)
from quantlab.analytics.risk import concentration, risk_summary, worst_periods
from quantlab.backtest.engine import BacktestResult


def jsonable(obj: Any) -> Any:
    """Recursively convert numpy/pandas scalars to JSON types; non-finite floats -> None."""
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [jsonable(v) for v in obj.tolist()]
    if isinstance(obj, pd.Timestamp | datetime):
        return None if pd.isna(obj) else obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating | float):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, np.bool_):
        return bool(obj)
    if obj is pd.NaT:
        return None
    return obj


def monthly_returns(equity: pd.Series) -> list[dict[str, Any]]:
    m = equity.resample("ME").last()
    first = equity.iloc[0]
    r = m.pct_change()
    r.iloc[0] = m.iloc[0] / first - 1
    return [{"year": d.year, "month": d.month, "return": float(v)} for d, v in r.items()]


def rolling_metrics(equity: pd.Series, window: int = 126) -> pd.DataFrame:
    r = simple_returns(equity)
    vol = r.rolling(window).std(ddof=1) * math.sqrt(252)
    sharpe = r.rolling(window).mean() / r.rolling(window).std(ddof=1) * math.sqrt(252)
    return pd.DataFrame({"rolling_vol": vol, "rolling_sharpe": sharpe})


def analyze_backtest(res: BacktestResult, factors: pd.DataFrame | None = None) -> dict[str, Any]:
    eq = res.equity
    rf = res.config.risk_free_rate
    perf = compute_performance(eq["equity"], res.benchmark, risk_free_rate=rf)
    gross = compute_performance(eq["gross_equity"], res.benchmark, risk_free_rate=rf)
    bench_perf = (
        compute_performance(res.benchmark, risk_free_rate=rf) if res.benchmark is not None else None
    )
    split_rows = [
        {"date": tx.date, "symbol": tx.symbol, "ratio": tx.price}
        for tx in res.ledger.transactions
        if tx.kind == "split"
    ]
    trades = trade_statistics(res.trades, pd.DataFrame(split_rows) if split_rows else None)
    costs = cost_and_turnover(eq)
    daily = simple_returns(eq["equity"])
    final_date = eq.index[-1]
    pos = res.positions
    final_w = (
        pos[pos["date"] == final_date].set_index("symbol")["weight"]
        if len(pos)
        else pd.Series(dtype=float)
    )
    exposure = eq["market_value"] / eq["equity"]
    dd = drawdown_episodes(eq["equity"], top=5)
    contrib = _contributions(res)
    factor_doc = None
    if factors is not None and len(factors.columns):
        from quantlab.analytics.factors import exposures

        factor_doc = exposures(simple_returns(eq["equity"]), factors)
    return jsonable(
        {
            "performance": perf.to_dict(),
            "gross_performance": gross.to_dict(),
            "benchmark_performance": bench_perf.to_dict() if bench_perf else None,
            "trades": trades.to_dict(),
            "costs": costs.to_dict(),
            "risk": risk_summary(daily, 0.95),
            "risk_99": risk_summary(daily, 0.99),
            "drawdowns": dd.to_dict("records"),
            "worst_periods": worst_periods(eq["equity"], 21, 5).to_dict("records"),
            "monthly_returns": monthly_returns(eq["equity"]),
            "exposure": {
                "average": float(exposure.mean()),
                "min": float(exposure.min()),
                "max": float(exposure.max()),
                "average_positions": float(eq["positions"].mean()),
            },
            "final_concentration": concentration(final_w),
            "final_holdings": final_w.sort_values(ascending=False).head(25).to_dict(),
            "contributions": contrib,
            "factor_exposures": factor_doc,
            "rejections": {
                "count": len(res.rejections),
                "by_reason": res.rejections["reason"].value_counts().to_dict()
                if len(res.rejections)
                else {},
            },
            "meta": res.meta,
        }
    )


def _contributions(res: BacktestResult) -> dict[str, Any]:
    """Per-symbol P&L attribution: sum of daily (qty * price change) + dividends - costs.

    Uses end-of-day holdings; P&L attributable to intraday fills at the open is assigned to
    the session's price change from the fill price, so totals reconcile with equity.
    """
    if len(res.trades) == 0:
        return {"top": [], "bottom": []}
    t = res.trades
    pnl: dict[str, float] = {}
    # Realized + unrealized P&L = proceeds - costs + final value per symbol.
    flows = t.groupby("symbol").apply(
        lambda g: float((-(g["quantity"] * g["price"]) - g["commission"]).sum()),
        include_groups=False,
    )
    for s, v in flows.items():
        pnl[str(s)] = v
    final_date = res.equity.index[-1]
    pos = res.positions[res.positions["date"] == final_date] if len(res.positions) else None
    if pos is not None:
        for row in pos.itertuples():
            pnl[row.symbol] = pnl.get(row.symbol, 0.0) + float(row.value)
    divs = [tx for tx in res.ledger.transactions if tx.kind == "dividend"]
    for tx in divs:
        pnl[tx.symbol or ""] = pnl.get(tx.symbol or "", 0.0) + tx.cash_delta
    s = pd.Series(pnl).sort_values()
    return {
        "method": "realized + unrealized P&L incl. dividends, net of commissions",
        "top": [{"symbol": k, "pnl": float(v)} for k, v in s.tail(10)[::-1].items()],
        "bottom": [{"symbol": k, "pnl": float(v)} for k, v in s.head(10).items()],
        "total": float(s.sum()),
    }
