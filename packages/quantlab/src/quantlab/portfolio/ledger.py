"""Transaction ledger for a simulated portfolio.

The ledger is the single source of truth for cash and positions. Every state change is
recorded as an immutable ``Transaction`` whose ``cash_delta`` fully explains the cash
movement, so the state can be rebuilt by replay and value conservation can be audited:

    cash == sum(t.cash_delta for t in transactions)

Conventions
-----------
* ``quantity`` is signed: positive buys, negative sells.
* ``price`` is the executed price *including* slippage; ``slippage`` records the cost of
  the slippage relative to the reference price (always >= 0 for adverse slippage).
* Commissions are paid in cash and recorded separately from notional.
* Dividends are credited per share held at the ex-date open (short positions pay them).
* Splits multiply the share count; fractional shares created by a split are retained
  (no cash-in-lieu), which conserves value exactly.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import pandas as pd

Kind = Literal["deposit", "withdrawal", "buy", "sell", "dividend", "split"]
EPS = 1e-9


class LedgerError(ValueError):
    """Base class for rejected ledger operations."""


class InsufficientCash(LedgerError):
    pass


class InsufficientPosition(LedgerError):
    pass


class InvalidOrder(LedgerError):
    pass


@dataclass(frozen=True)
class Transaction:
    seq: int
    date: pd.Timestamp
    kind: Kind
    symbol: str | None
    quantity: float
    price: float
    cash_delta: float
    commission: float = 0.0
    slippage: float = 0.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        return d


@dataclass
class Ledger:
    allow_short: bool = False
    allow_negative_cash: bool = False
    cash: float = 0.0
    positions: dict[str, float] = field(default_factory=dict)
    transactions: list[Transaction] = field(default_factory=list)

    # ------------------------------------------------------------------ helpers
    def _record(self, **kw: Any) -> Transaction:
        tx = Transaction(seq=len(self.transactions), **kw)
        self.transactions.append(tx)
        return tx

    def quantity(self, symbol: str) -> float:
        return self.positions.get(symbol, 0.0)

    def _set_qty(self, symbol: str, qty: float) -> None:
        if abs(qty) < EPS:
            self.positions.pop(symbol, None)
        else:
            self.positions[symbol] = qty

    # ------------------------------------------------------------------ cash
    def deposit(self, date: pd.Timestamp, amount: float, note: str = "") -> Transaction:
        if not math.isfinite(amount) or amount <= 0:
            raise InvalidOrder("deposit must be positive and finite")
        self.cash += amount
        return self._record(
            date=date,
            kind="deposit",
            symbol=None,
            quantity=0.0,
            price=0.0,
            cash_delta=amount,
            note=note,
        )

    def withdraw(self, date: pd.Timestamp, amount: float, note: str = "") -> Transaction:
        if not math.isfinite(amount) or amount <= 0:
            raise InvalidOrder("withdrawal must be positive and finite")
        if amount > self.cash + EPS and not self.allow_negative_cash:
            raise InsufficientCash(f"withdrawal {amount:.2f} exceeds cash {self.cash:.2f}")
        self.cash -= amount
        return self._record(
            date=date,
            kind="withdrawal",
            symbol=None,
            quantity=0.0,
            price=0.0,
            cash_delta=-amount,
            note=note,
        )

    # ------------------------------------------------------------------ trading
    def trade(
        self,
        date: pd.Timestamp,
        symbol: str,
        quantity: float,
        price: float,
        commission: float = 0.0,
        slippage: float = 0.0,
        note: str = "",
    ) -> Transaction:
        if not (math.isfinite(quantity) and math.isfinite(price) and math.isfinite(commission)):
            raise InvalidOrder("non-finite order values")
        if abs(quantity) < EPS:
            raise InvalidOrder("zero-quantity order")
        if price <= 0:
            raise InvalidOrder(f"non-positive execution price {price}")
        if commission < 0:
            raise InvalidOrder("negative commission")
        held = self.quantity(symbol)
        new_qty = held + quantity
        if not self.allow_short and new_qty < -EPS:
            raise InsufficientPosition(
                f"sell {-quantity:g} {symbol} exceeds holding {held:g} (shorting disabled)"
            )
        cash_delta = -quantity * price - commission
        if self.cash + cash_delta < -EPS and not self.allow_negative_cash:
            raise InsufficientCash(
                f"order needs {-cash_delta:.2f} but only {self.cash:.2f} cash is available"
            )
        self.cash += cash_delta
        self._set_qty(symbol, new_qty)
        return self._record(
            date=date,
            kind="buy" if quantity > 0 else "sell",
            symbol=symbol,
            quantity=quantity,
            price=price,
            cash_delta=cash_delta,
            commission=commission,
            slippage=slippage,
            note=note,
        )

    # ------------------------------------------------------------------ corporate actions
    def dividend(self, date: pd.Timestamp, symbol: str, per_share: float) -> Transaction | None:
        qty = self.quantity(symbol)
        if abs(qty) < EPS:
            return None
        amount = qty * per_share
        self.cash += amount
        return self._record(
            date=date,
            kind="dividend",
            symbol=symbol,
            quantity=qty,
            price=per_share,
            cash_delta=amount,
        )

    def split(self, date: pd.Timestamp, symbol: str, ratio: float) -> Transaction | None:
        if not math.isfinite(ratio) or ratio <= 0:
            raise InvalidOrder("split ratio must be positive")
        qty = self.quantity(symbol)
        if abs(qty) < EPS:
            return None
        self._set_qty(symbol, qty * ratio)
        return self._record(
            date=date,
            kind="split",
            symbol=symbol,
            quantity=qty * (ratio - 1),
            price=ratio,
            cash_delta=0.0,
        )

    # ------------------------------------------------------------------ valuation
    def market_value(self, prices: Mapping[str, float]) -> float:
        total = 0.0
        for sym, qty in self.positions.items():
            p = prices.get(sym)
            if p is None or not math.isfinite(p):
                raise LedgerError(f"no valuation price for held symbol {sym}")
            total += qty * p
        return total

    def equity(self, prices: Mapping[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def gross_exposure(self, prices: Mapping[str, float]) -> float:
        return sum(abs(q * prices[s]) for s, q in self.positions.items())

    # ------------------------------------------------------------------ audit
    def check_invariants(self) -> None:
        """Raise if the recorded transactions do not explain the current state."""
        replayed = replay(
            self.transactions,
            allow_short=self.allow_short,
            allow_negative_cash=self.allow_negative_cash,
        )
        if not math.isclose(replayed.cash, self.cash, rel_tol=1e-9, abs_tol=1e-6):
            raise LedgerError(f"cash {self.cash} != replayed {replayed.cash}")
        keys = set(replayed.positions) | set(self.positions)
        for k in keys:
            if not math.isclose(replayed.quantity(k), self.quantity(k), rel_tol=1e-9, abs_tol=1e-9):
                raise LedgerError(f"position mismatch for {k}")
        if not self.allow_negative_cash and self.cash < -1e-6:
            raise LedgerError("negative cash without margin policy")
        if not self.allow_short and any(q < -EPS for q in self.positions.values()):
            raise LedgerError("short position without short policy")

    def snapshot(self, date: pd.Timestamp, prices: Mapping[str, float]) -> dict[str, Any]:
        mv = {s: q * prices[s] for s, q in self.positions.items()}
        eq = self.cash + sum(mv.values())
        return {
            "date": date.isoformat(),
            "cash": self.cash,
            "equity": eq,
            "positions": [
                {
                    "symbol": s,
                    "quantity": q,
                    "price": prices[s],
                    "value": mv[s],
                    "weight": mv[s] / eq if eq else None,
                }
                for s, q in sorted(self.positions.items())
            ],
        }


def replay(
    transactions: Iterable[Transaction],
    *,
    allow_short: bool = False,
    allow_negative_cash: bool = False,
) -> Ledger:
    """Rebuild a ledger's state purely from its transaction log."""
    led = Ledger(allow_short=allow_short, allow_negative_cash=allow_negative_cash)
    for tx in transactions:
        led.cash += tx.cash_delta
        if tx.kind in ("buy", "sell"):
            led._set_qty(tx.symbol or "", led.quantity(tx.symbol or "") + tx.quantity)
        elif tx.kind == "split":
            led._set_qty(tx.symbol or "", led.quantity(tx.symbol or "") * tx.price)
        led.transactions.append(tx)
    return led
