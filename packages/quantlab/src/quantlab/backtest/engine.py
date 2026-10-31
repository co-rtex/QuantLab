"""Event-driven daily backtest engine.

Timeline for each session ``k`` (all prices are raw, as traded):

1. **Open** — corporate actions placed on session ``k`` are applied to holdings: those
   ex-dated ``k``, plus any whose ex-date fell on a day the symbol printed no bar (see
   ``quantlab.data.adjust``), composed in ex-date order into one dividend on the share
   count held before the open, then one split. Positions in symbols that have printed no
   bar for ``delist_after_missing`` sessions are liquidated at their last close.
2. **Open execution** (``execution="next_open"``) — orders decided at the previous
   rebalance close are filled at ``open[k]`` adjusted for slippage. Sells run before buys.
3. **Close execution** (``execution="next_close"``) — as above but at ``close[k]``.
4. **Mark-to-market** at ``close[k]`` (last known close for symbols with a gap).
5. **Decision** — on rebalance dates the strategy receives a ``MarketView`` containing only
   bars dated ``<= k`` with corporate actions placed on sessions ``<= k`` and returns target
   weights. Orders are sized against ``close[k]`` and queued for the *next* session, so no
   order is ever filled at a price the strategy could not have known at decision time.

Orders that cannot be filled (no bar on the execution date, insufficient cash, sells
exceeding holdings) are rejected and recorded with a reason; buys that exceed available
cash are reduced to the affordable quantity and flagged as partial fills.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantlab.data.adjust import PointInTimeAdjuster, total_returns
from quantlab.data.schema import wide
from quantlab.portfolio.ledger import Ledger, LedgerError
from quantlab.strategies.base import MarketView, Strategy

ENGINE_VERSION = "1.2.0"


class BacktestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: date | None = None
    end: date | None = None
    initial_capital: float = Field(1_000_000.0, gt=0, le=1e12)
    rebalance: Literal["daily", "weekly", "monthly", "quarterly"] = "monthly"
    execution: Literal["next_open", "next_close"] = "next_open"
    commission_bps: float = Field(1.0, ge=0, le=500)
    commission_min: float = Field(0.0, ge=0, le=1000)
    slippage_bps: float = Field(5.0, ge=0, le=500)
    fractional_shares: bool = False
    max_weight: float = Field(1.0, gt=0, le=1)
    max_gross_exposure: float = Field(1.0, gt=0, le=1)
    min_trade_value: float = Field(0.0, ge=0)
    universe: list[str] | None = None
    benchmark: str | None = None
    risk_free_rate: float = Field(0.0, ge=-0.05, le=0.25)
    delist_after_missing: int = Field(5, ge=1, le=63)

    @model_validator(mode="after")
    def _dates(self) -> BacktestConfig:
        for d in (self.start, self.end):
            if d is not None and not date(1900, 1, 1) <= d <= date(2200, 12, 31):
                raise ValueError("dates must be between 1900-01-01 and 2200-12-31")
        if self.start and self.end and self.start >= self.end:
            raise ValueError("start must be before end")
        return self

    def with_costs(self, multiplier: float) -> BacktestConfig:
        return self.model_copy(
            update={
                "commission_bps": self.commission_bps * multiplier,
                "commission_min": self.commission_min * multiplier,
                "slippage_bps": self.slippage_bps * multiplier,
            }
        )


class BacktestCancelled(RuntimeError):
    pass


class BacktestError(RuntimeError):
    pass


@dataclass
class Order:
    decision_date: pd.Timestamp
    symbol: str
    quantity: float
    reference_price: float
    scaled: bool = False


@dataclass
class BacktestResult:
    config: BacktestConfig
    strategy: dict[str, Any]
    equity: pd.DataFrame  # date-indexed daily records
    trades: pd.DataFrame
    rejections: pd.DataFrame
    positions: pd.DataFrame  # long format: date, symbol, quantity, price, value, weight
    weights_target: pd.DataFrame  # long format decisions: date, symbol, weight
    benchmark: pd.Series | None
    ledger: Ledger
    meta: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Stable hash of the economic outputs, used for reproducibility checks."""
        h = hashlib.sha256()
        h.update(np.round(self.equity["equity"].to_numpy(), 6).tobytes())
        if len(self.trades):
            h.update(self.trades["symbol"].str.cat(sep="|").encode())
            h.update(np.round(self.trades[["quantity", "price"]].to_numpy(), 6).tobytes())
        return h.hexdigest()


def rebalance_mask(dates: pd.DatetimeIndex, freq: str) -> np.ndarray:
    """True on the last session of each period (and on the first session)."""
    if len(dates) == 0:
        return np.zeros(0, dtype=bool)
    if freq == "daily":
        return np.ones(len(dates), dtype=bool)
    if freq == "weekly":
        key = dates.isocalendar().year.to_numpy() * 100 + dates.isocalendar().week.to_numpy()
    elif freq == "monthly":
        key = dates.year * 100 + dates.month
    elif freq == "quarterly":
        key = dates.year * 10 + (dates.month - 1) // 3
    else:
        raise ValueError(freq)
    key = np.asarray(key)
    mask = np.r_[key[1:] != key[:-1], True]
    mask[0] = True
    return mask


def run_backtest(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    strategy: Strategy,
    config: BacktestConfig,
    *,
    on_progress: Callable[[float], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    record_positions: bool = True,
) -> BacktestResult:
    universe = sorted({s.upper() for s in config.universe}) if config.universe else None
    bench = config.benchmark.upper() if config.benchmark else None
    known = set(bars["symbol"].unique())
    if universe is None:
        universe = sorted(known - ({bench} if bench else set()))
    unknown = set(universe) - known
    if unknown:
        raise BacktestError(f"no data for symbols: {sorted(unknown)[:10]}")
    keep = set(universe) | ({bench} if bench else set())
    bars = bars[bars["symbol"].isin(keep)]
    if config.end:
        bars = bars[bars["date"] <= pd.Timestamp(config.end)]
    if len(bars) == 0:
        raise BacktestError("no market data for the requested universe and dates")
    # Symbols that only list after the window cannot be traded in it. Excluding them uses
    # nothing a researcher at the window's end would not know.
    present = set(bars["symbol"].unique())
    not_listed = sorted(set(universe) - present)
    universe = [s for s in universe if s in present]
    if not universe:
        raise BacktestError("no symbol in the universe has data in the backtest window")
    acts = actions[actions["symbol"].isin(keep)]
    if config.end:
        acts = acts[acts["ex_date"] <= pd.Timestamp(config.end)]

    close_all = wide(bars, "close")
    dates = close_all.index
    close_raw = close_all[universe]
    open_raw = wide(bars, "open").reindex(index=dates, columns=universe)
    volume = wide(bars, "volume").reindex(index=dates, columns=universe)
    adjuster = PointInTimeAdjuster(close_raw, acts[acts["symbol"].isin(universe)])
    adj_full = close_raw.to_numpy() * adjuster.multiplier(None)
    rets = total_returns(bars[bars["symbol"].isin(universe)], acts).reindex(
        index=dates, columns=universe
    )
    splits, divs = adjuster.splits, adjuster.dividends
    C = close_raw.to_numpy()
    O = open_raw.to_numpy()
    has_bar = np.isfinite(C)
    missing_run = np.zeros(len(universe), dtype=np.int64)
    col = {s: j for j, s in enumerate(universe)}
    placement = adjuster.placed["placement"].value_counts()
    action_counts = {
        "on_ex_date": int(placement.get("ex_date", 0)),
        "applied_next_session": int(placement.get("next_bar", 0)),
        "outside_history": int(
            placement.get("before_history", 0) + placement.get("after_history", 0)
        ),
    }

    start_pos = 0 if config.start is None else int(dates.searchsorted(pd.Timestamp(config.start)))
    if start_pos >= len(dates) - 1:
        raise BacktestError("backtest window contains fewer than two sessions")
    trade_dates = dates[start_pos:]
    rebal = rebalance_mask(trade_dates, config.rebalance)
    warmup = strategy.warmup

    ledger = Ledger()
    ledger.deposit(trade_dates[0], config.initial_capital, note="initial capital")
    last_px = np.full(len(universe), np.nan)
    pending: list[Order] = []
    records: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    pos_rows: list[tuple[pd.Timestamp, str, float, float]] = []
    decisions: list[tuple[pd.Timestamp, str, float]] = []
    cum = {"commission": 0.0, "slippage": 0.0, "dividends": 0.0}
    slip = config.slippage_bps / 1e4
    comm = config.commission_bps / 1e4

    def prorate_buys(k: int, px_row: np.ndarray, orders: list[Order]) -> None:
        """Scale all buys by the same factor when the cash left after sells cannot fund
        them, instead of filling whichever symbols sort first."""
        cash = ledger.cash
        for o in orders:
            if o.quantity < 0 and math.isfinite(px_row[col[o.symbol]]):
                q = min(-o.quantity, ledger.quantity(o.symbol))
                cash += q * px_row[col[o.symbol]] * (1 - slip) * (1 - comm)
        need = sum(
            o.quantity * px_row[col[o.symbol]] * (1 + slip) * (1 + comm) + config.commission_min
            for o in orders
            if o.quantity > 0 and math.isfinite(px_row[col[o.symbol]])
        )
        if need > cash > 0:
            scale = cash / need
            for o in orders:
                if o.quantity > 0:
                    o.quantity *= scale
                    o.scaled = True

    def fill(k: int, px_row: np.ndarray, orders: list[Order]) -> float:
        d = dates[k]
        traded = 0.0
        prorate_buys(k, px_row, orders)
        for o in sorted(orders, key=lambda o: (o.quantity > 0, o.symbol)):
            j = col[o.symbol]
            ref = px_row[j]
            if not math.isfinite(ref):
                rejects.append(
                    {
                        "date": d,
                        "decision_date": o.decision_date,
                        "symbol": o.symbol,
                        "quantity": o.quantity,
                        "reason": "no_price",
                    }
                )
                continue
            qty = o.quantity
            if qty < 0:
                qty = max(qty, -ledger.quantity(o.symbol))
                if qty == 0:
                    continue
            if qty > 0 and not config.fractional_shares:
                qty = math.floor(qty + 1e-9)
                if qty == 0:
                    continue
            px = ref * (1 + slip) if qty > 0 else ref * (1 - slip)
            partial = o.scaled
            if qty > 0:
                # Shrink buys to what the cash balance can fund, including commission.
                affordable = ledger.cash / (px * (1 + comm)) if px > 0 else 0.0
                if config.commission_min:
                    affordable = min(affordable, (ledger.cash - config.commission_min) / px)
                if not config.fractional_shares:
                    affordable = math.floor(affordable + 1e-9)
                if affordable < qty - 1e-9:
                    qty, partial = max(affordable, 0.0), True
                if qty <= 0:
                    rejects.append(
                        {
                            "date": d,
                            "decision_date": o.decision_date,
                            "symbol": o.symbol,
                            "quantity": o.quantity,
                            "reason": "insufficient_cash",
                        }
                    )
                    continue
            notional = abs(qty) * px
            commission = max(config.commission_min, notional * comm) if notional else 0.0
            slip_cost = abs(qty) * abs(px - ref)
            try:
                ledger.trade(d, o.symbol, qty, px, commission=commission, slippage=slip_cost)
            except LedgerError as exc:
                rejects.append(
                    {
                        "date": d,
                        "decision_date": o.decision_date,
                        "symbol": o.symbol,
                        "quantity": o.quantity,
                        "reason": type(exc).__name__,
                    }
                )
                continue
            cum["commission"] += commission
            cum["slippage"] += slip_cost
            traded += notional
            trades.append(
                {
                    "date": d,
                    "decision_date": o.decision_date,
                    "symbol": o.symbol,
                    "side": "buy" if qty > 0 else "sell",
                    "quantity": qty,
                    "reference_price": ref,
                    "price": px,
                    "notional": notional,
                    "commission": commission,
                    "slippage_cost": slip_cost,
                    "partial": partial,
                }
            )
        return traded

    n_steps = len(trade_dates)
    for step, k in enumerate(range(start_pos, len(dates))):
        if should_cancel is not None and step % 20 == 0 and should_cancel():
            raise BacktestCancelled(f"cancelled at {dates[k].date()}")
        d = dates[k]
        traded = 0.0
        # 1. corporate actions at the open
        for sym in list(ledger.positions):
            j = col[sym]
            if divs[k, j] > 0:
                tx = ledger.dividend(d, sym, divs[k, j])
                if tx:
                    cum["dividends"] += tx.cash_delta
            if splits[k, j] != 1.0:
                ledger.split(d, sym, splits[k, j])
                last_px[j] = last_px[j] / splits[k, j]
        # orders decided before a split are expressed in pre-split shares; restate them
        for o in pending:
            ratio = splits[k, col[o.symbol]]
            if ratio != 1.0:
                o.quantity *= ratio
                o.reference_price /= ratio
        # delisting: a held symbol that has printed no bar for `delist_after_missing`
        # consecutive sessions is liquidated at its last close. The rule only looks at the
        # past, so it cannot know in advance that a series has ended.
        missing_run = np.where(has_bar[k], 0, missing_run + 1)
        for sym in list(ledger.positions):
            j = col[sym]
            if missing_run[j] >= config.delist_after_missing and math.isfinite(last_px[j]):
                q = ledger.quantity(sym)
                ledger.trade(d, sym, -q, last_px[j], note="delisted: liquidated at last close")
                trades.append(
                    {
                        "date": d,
                        "decision_date": d,
                        "symbol": sym,
                        "side": "sell",
                        "quantity": -q,
                        "reference_price": last_px[j],
                        "price": last_px[j],
                        "notional": abs(q) * last_px[j],
                        "commission": 0.0,
                        "slippage_cost": 0.0,
                        "partial": False,
                        "note": "delisting",
                    }
                )
                traded += abs(q) * last_px[j]
        # 2/3. execute orders queued at the previous decision
        if pending:
            traded += fill(k, O[k] if config.execution == "next_open" else C[k], pending)
            pending = []
        # 4. mark to market
        row = C[k]
        last_px = np.where(np.isfinite(row), row, last_px)
        prices = {s: float(last_px[col[s]]) for s in ledger.positions}
        mv = ledger.market_value(prices)
        equity = ledger.cash + mv
        records.append(
            {
                "date": d,
                "equity": equity,
                "cash": ledger.cash,
                "market_value": mv,
                "positions": len(ledger.positions),
                "traded_notional": traded,
                "cum_commission": cum["commission"],
                "cum_slippage": cum["slippage"],
                "cum_dividends": cum["dividends"],
            }
        )
        if record_positions:
            for s, q in ledger.positions.items():
                pos_rows.append((d, s, q, prices[s]))
        # 5. decision at the close
        if rebal[step] and k + 1 < len(dates) and k + 1 >= warmup:
            scale = adjuster.scale_as_of(k)
            view_close = pd.DataFrame(
                adj_full[: k + 1] / scale, index=dates[: k + 1], columns=universe
            )
            holdings = (
                pd.Series(
                    {s: q * prices[s] / equity for s, q in ledger.positions.items()}, dtype=float
                ).reindex(universe, fill_value=0.0)
                if equity > 0
                else pd.Series(0.0, index=universe)
            )
            view = MarketView(
                date=d,
                close=view_close,
                returns=rets.iloc[: k + 1],
                volume=volume.iloc[: k + 1],
                tradable=pd.Series(has_bar[k], index=universe),
                holdings=holdings,
                raw_close=close_raw.iloc[: k + 1],
            )
            target = strategy.target_weights(view)
            if target is not None:
                target = _sanitize(target, view, config)
                for s, w in target.items():
                    decisions.append((d, s, float(w)))
                pending = _orders(d, target, ledger, row, last_px, col, equity, config)
        if on_progress is not None and (step % 25 == 0 or step == n_steps - 1):
            on_progress((step + 1) / n_steps)

    ledger.check_invariants()
    eq = pd.DataFrame(records).set_index("date")
    eq["gross_equity"] = eq["equity"] + eq["cum_commission"] + eq["cum_slippage"]
    eq["turnover"] = eq["traded_notional"] / eq["equity"].shift(1).fillna(config.initial_capital)
    trades_df = pd.DataFrame(trades)
    if len(trades_df) and "note" not in trades_df:
        trades_df["note"] = None
    rej_df = pd.DataFrame(
        rejects, columns=["date", "decision_date", "symbol", "quantity", "reason"]
    )
    pos_df = pd.DataFrame(pos_rows, columns=["date", "symbol", "quantity", "price"])
    if len(pos_df):
        pos_df["value"] = pos_df["quantity"] * pos_df["price"]
        pos_df["weight"] = pos_df["value"] / pos_df["date"].map(eq["equity"])
    bench_series = None
    if bench is not None and bench in close_all.columns:
        b_bars = bars[bars["symbol"] == bench]
        b_ret = total_returns(b_bars, acts)[bench].reindex(trade_dates).fillna(0.0)
        b_ret.iloc[0] = 0.0
        bench_series = config.initial_capital * (1 + b_ret).cumprod()
        bench_series.name = bench
    result = BacktestResult(
        config=config,
        strategy=strategy.describe(),
        equity=eq,
        trades=trades_df,
        rejections=rej_df,
        positions=pos_df,
        weights_target=pd.DataFrame(decisions, columns=["date", "symbol", "weight"]),
        benchmark=bench_series,
        ledger=ledger,
        meta={
            "engine_version": ENGINE_VERSION,
            "universe": universe,
            "benchmark": bench,
            "sessions": n_steps,
            "not_listed_in_window": not_listed,
            "corporate_actions": action_counts,
            "first_session": str(trade_dates[0].date()),
            "last_session": str(trade_dates[-1].date()),
        },
    )
    return result


def _sanitize(target: pd.Series, view: MarketView, config: BacktestConfig) -> pd.Series:
    t = pd.to_numeric(target, errors="coerce").astype(float)
    t = t[np.isfinite(t) & (t > 0)]
    t = t[t.index.isin(view.universe)]
    t = t.clip(upper=config.max_weight)
    total = t.sum()
    if total > config.max_gross_exposure:
        t = t * (config.max_gross_exposure / total)
    return t.sort_index()


def _orders(
    d: pd.Timestamp,
    target: pd.Series,
    ledger: Ledger,
    close_row: np.ndarray,
    last_px: np.ndarray,
    col: dict[str, int],
    equity: float,
    config: BacktestConfig,
) -> list[Order]:
    orders: list[Order] = []
    for sym in sorted(set(target.index) | set(ledger.positions)):
        j = col[sym]
        ref = close_row[j] if math.isfinite(close_row[j]) else last_px[j]
        if not math.isfinite(ref) or ref <= 0:
            continue
        want = float(target.get(sym, 0.0)) * equity / ref
        if not config.fractional_shares:
            want = math.floor(want + 1e-9)
        delta = want - ledger.quantity(sym)
        if abs(delta) < 1e-9 or abs(delta) * ref < config.min_trade_value:
            continue
        orders.append(Order(d, sym, delta, float(ref)))
    return orders
