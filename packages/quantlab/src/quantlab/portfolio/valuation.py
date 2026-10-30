"""Value a manually managed portfolio from its transaction log and market data.

Daily replay, for each trading session from the first transaction:

1. corporate actions at the open (dividend on held shares, then split),
2. that day's transactions in sequence order (trades are assumed at the given price,
   by default the session close),
3. mark-to-market at the close (last known close if a symbol has no bar that day).

Any transaction that the ledger rejects (insufficient cash, selling more than held,
non-positive price) raises ``LedgerError`` naming the offending entry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from quantlab.data.adjust import event_matrices
from quantlab.data.schema import wide
from quantlab.portfolio.ledger import Ledger, LedgerError


@dataclass(frozen=True)
class Entry:
    seq: int
    date: date
    kind: str  # deposit | withdrawal | buy | sell
    symbol: str | None = None
    quantity: float = 0.0
    price: float | None = None
    commission: float = 0.0
    amount: float = 0.0


@dataclass
class Valuation:
    equity: pd.DataFrame  # date-indexed: equity, cash, market_value, net_flows
    holdings: pd.DataFrame  # final holdings: symbol, quantity, price, value, weight
    ledger: Ledger
    resolved_prices: dict[int, float]


def close_on(bars_close: pd.DataFrame, symbol: str, d: date) -> float | None:
    ts = pd.Timestamp(d)
    if symbol not in bars_close.columns or ts not in bars_close.index:
        return None
    v = bars_close.at[ts, symbol]
    return None if pd.isna(v) else float(v)


def value_portfolio(
    entries: list[Entry], bars: pd.DataFrame, actions: pd.DataFrame, end: date | None = None
) -> Valuation:
    if not entries:
        raise LedgerError("portfolio has no transactions")
    entries = sorted(entries, key=lambda e: (e.date, e.seq))
    symbols = sorted({e.symbol for e in entries if e.symbol})
    close = (
        wide(bars[bars["symbol"].isin(symbols)], "close")
        if symbols
        else pd.DataFrame(index=pd.DatetimeIndex(sorted(bars["date"].unique())))
    )
    calendar = pd.DatetimeIndex(sorted(bars["date"].unique()))
    close = close.reindex(calendar)
    first = pd.Timestamp(entries[0].date)
    last = pd.Timestamp(end) if end else calendar[-1]
    sessions = calendar[(calendar >= first) & (calendar <= last)]
    off_calendar = [e for e in entries if pd.Timestamp(e.date) not in calendar]
    if off_calendar:
        raise LedgerError(
            f"transaction #{off_calendar[0].seq} on {off_calendar[0].date} is not "
            "a trading session in this dataset"
        )
    if symbols:
        splits, divs = event_matrices(close, actions[actions["symbol"].isin(symbols)])
    else:
        splits = divs = np.empty((len(calendar), 0))
    col = {s: j for j, s in enumerate(close.columns)}
    by_day: dict[pd.Timestamp, list[Entry]] = {}
    for e in entries:
        by_day.setdefault(pd.Timestamp(e.date), []).append(e)

    led = Ledger()
    last_px = np.full(len(close.columns), np.nan)
    resolved: dict[int, float] = {}
    rows = []
    C = close.to_numpy()
    for d in sessions:
        k = calendar.get_loc(d)
        for sym in list(led.positions):
            j = col[sym]
            if divs[k, j] > 0:
                led.dividend(d, sym, float(divs[k, j]))
            if splits[k, j] != 1.0:
                led.split(d, sym, float(splits[k, j]))
                last_px[j] = last_px[j] / splits[k, j]
        flows = 0.0
        for e in by_day.get(d, []):
            try:
                if e.kind == "deposit":
                    led.deposit(d, e.amount, note=f"#{e.seq}")
                    flows += e.amount
                elif e.kind == "withdrawal":
                    led.withdraw(d, e.amount, note=f"#{e.seq}")
                    flows -= e.amount
                elif e.kind in ("buy", "sell"):
                    assert e.symbol is not None
                    px = e.price if e.price is not None else close_on(close, e.symbol, e.date)
                    if px is None:
                        raise LedgerError(f"no price for {e.symbol} on {e.date}")
                    resolved[e.seq] = px
                    qty = abs(e.quantity) if e.kind == "buy" else -abs(e.quantity)
                    led.trade(d, e.symbol, qty, px, commission=e.commission, note=f"#{e.seq}")
                else:
                    raise LedgerError(f"unknown transaction kind {e.kind!r}")
            except LedgerError as exc:
                raise LedgerError(f"transaction #{e.seq} ({e.kind} on {e.date}): {exc}") from exc
        row = C[k]
        last_px = np.where(np.isfinite(row), row, last_px)
        prices = {s: float(last_px[col[s]]) for s in led.positions}
        mv = sum(q * prices[s] for s, q in led.positions.items() if math.isfinite(prices[s]))
        rows.append(
            {
                "date": d,
                "equity": led.cash + mv,
                "cash": led.cash,
                "market_value": mv,
                "net_flows": flows,
            }
        )
    led.check_invariants()
    eq = pd.DataFrame(rows).set_index("date")
    final = {s: float(last_px[col[s]]) for s in led.positions}
    total = float(eq["equity"].iloc[-1])
    holdings = pd.DataFrame(
        [
            {
                "symbol": s,
                "quantity": q,
                "price": final[s],
                "value": q * final[s],
                "weight": q * final[s] / total if total else None,
            }
            for s, q in sorted(led.positions.items())
        ],
        columns=["symbol", "quantity", "price", "value", "weight"],
    )
    return Valuation(eq, holdings, led, resolved)


def time_weighted_returns(equity: pd.DataFrame) -> pd.Series:
    """Daily returns that strip out deposits and withdrawals (flows at the close)."""
    e = equity["equity"]
    prev = e.shift(1)
    r = (e - equity["net_flows"]) / prev - 1
    return r.iloc[1:][prev.iloc[1:] > 0]


def summary(val: Valuation) -> dict[str, Any]:
    r = time_weighted_returns(val.equity)
    growth = float((1 + r).prod()) if len(r) else 1.0
    return {
        "equity": float(val.equity["equity"].iloc[-1]),
        "cash": float(val.equity["cash"].iloc[-1]),
        "net_contributions": float(val.equity["net_flows"].sum()),
        "time_weighted_return": growth - 1,
        "sessions": len(val.equity),
    }
