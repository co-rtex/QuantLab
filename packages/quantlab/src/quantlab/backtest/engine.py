"""Event-driven daily backtest engine.

Timeline for each session ``k`` (all prices are raw, as traded):

1. **Open** — corporate actions placed on session ``k`` are applied to holdings: those
   ex-dated ``k``, plus any whose ex-date fell on a day the symbol printed no bar (see
   ``quantlab.data.adjust``), composed in ex-date order into one dividend on the share
   count held before the open, then one split. **Delistings:** with an instrument master
   (``instruments``), a position is closed on the first session after its
   ``delisting_date`` at last close x (1 + ``delisting_return``), or at the last close
   when the return is unknown. From its delisting date it is not offered to the strategy,
   and actions dated after that date are not applied (the delisting return includes
   them). Without a master (or for securities the master does not delist), positions in
   symbols that have printed no bar for ``delist_after_missing`` sessions are liquidated
   at their last close; a security the master delists is left to the master even
   through a halt.
2. **Open execution** (``execution="next_open"``) — orders decided at the previous
   rebalance close are filled at ``open[k]`` adjusted for slippage. Sells run before buys.
3. **Close execution** (``execution="next_close"``) — as above but at ``close[k]``.
4. **Mark-to-market** at ``close[k]`` (last known close for symbols with a gap). A close
   printed with zero volume marks the position too, but it is a stale mark.
5. **Decision** — on rebalance dates the strategy receives a ``MarketView`` containing only
   bars dated ``<= k`` with corporate actions placed on sessions ``<= k`` and returns target
   weights. Orders are sized against ``close[k]`` and queued for the *next* session, so no
   order is ever filled at a price the strategy could not have known at decision time.

Orders that cannot be filled (no bar on the execution date, a bar with zero volume,
insufficient cash, sells exceeding holdings) are rejected and recorded with a reason; buys
that exceed available cash are reduced to the affordable quantity and flagged as partial
fills, and a buy reduced to nothing is rejected as ``insufficient_cash`` (#184). With whole
shares, a buy under one share as it reaches the fill (after split restatement) is rejected as
``below_one_share`` and takes no part in pro-rating (#187).
"""

from __future__ import annotations

import hashlib
import math
from array import array
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantlab.data.adjust import PointInTimeAdjuster, total_returns
from quantlab.data.identity import PERFORMANCE_REASONS, normalize_master
from quantlab.data.schema import wide
from quantlab.portfolio.ledger import EPS as LEDGER_EPS
from quantlab.portfolio.ledger import Ledger, LedgerError
from quantlab.strategies.base import MarketView, Strategy

ENGINE_VERSION = "1.5.0"


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
    # Return assumed for a performance delisting (bankruptcy, liquidation, noncompliance)
    # whose delisting return is unknown; None keeps the optimistic last-close exit. Shumway
    # (1997) estimates about -0.30 for NYSE/AMEX performance delistings.
    performance_delisting_return: float | None = Field(None, ge=-1.0, le=0.0)

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


class _Rows:
    """Result rows kept by column while the simulation runs (#163).

    A list of one dict per trade cost about 830 bytes a row: about 1 GB for 400 names
    rebalanced daily over 19 years, which is what exhausted a 1.5 GB worker. Float columns
    are C arrays of float64 (the values the frame would hold anyway). The others stay in
    lists, so pandas infers their dtypes exactly as it did from the rows.
    """

    def __init__(self, columns: tuple[str, ...], floats: frozenset[str]) -> None:
        self.columns = columns
        self._cols: list[Any] = [array("d") if c in floats else [] for c in columns]
        self.n = 0

    def append(self, *values: Any) -> None:
        for col, v in zip(self._cols, values, strict=True):
            col.append(v)
        self.n += 1

    def frame(self) -> pd.DataFrame:
        """The rows as a frame. The columns are released, so call this once, at the end."""
        cols, self._cols = self._cols, []
        if not self.n:  # as an empty row list gave: object columns
            return pd.DataFrame(columns=list(self.columns))
        return pd.DataFrame(
            {
                name: np.frombuffer(col, dtype=np.float64) if isinstance(col, array) else col
                for name, col in zip(self.columns, cols, strict=True)
            }
        )


TRADE_COLUMNS = (
    "date",
    "decision_date",
    "symbol",
    "side",
    "quantity",
    "reference_price",
    "price",
    "notional",
    "commission",
    "slippage_cost",
    "partial",
    "note",
)
_TRADE_FLOATS = frozenset({"reference_price", "price", "notional", "commission", "slippage_cost"})


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
    instruments: pd.DataFrame | None = None,
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
    # Delisting date and return per universe symbol, from the instrument master. The
    # date is public on the day, so acting on the next session uses no future data. The
    # return (final value vs last close) is booked at that point, as is standard.
    delist_at: dict[str, tuple[pd.Timestamp, float]] = {}
    fallback: set[str] = set()  # delistings priced with performance_delisting_return
    master_unmatched = 0
    if instruments is not None and len(instruments):
        master = normalize_master(instruments).set_index("instrument_id")
        master_unmatched = int((~master.index.isin(list(known))).sum())
        for sym in universe:
            if sym in master.index and pd.notna(master.at[sym, "delisting_date"]):
                ret = master.at[sym, "delisting_return"]
                value = float(ret) if pd.notna(ret) else math.nan
                if (
                    math.isnan(value)
                    and config.performance_delisting_return is not None
                    and master.at[sym, "delisting_reason"] in PERFORMANCE_REASONS
                ):
                    value = config.performance_delisting_return
                    fallback.add(sym)
                delist_at[sym] = (pd.Timestamp(master.at[sym, "delisting_date"]), value)
    if delist_at:
        # A delisting return already includes distributions after the delisting (CRSP
        # DLRET), so actions dated after a security's delisting date are not applied.
        cutoff = acts["symbol"].map({s: w for s, (w, _) in delist_at.items()})
        acts = acts[cutoff.isna() | (acts["ex_date"] <= cutoff)]

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
    # A bar with zero volume printed an open and a close nobody traded at (#149), so nothing
    # fills on it: an order meets it as it meets a session without a bar (rejected, here
    # as `no_volume`). The fill rule alone blocks the escape from a halt. The strategy is
    # still offered the symbol (it has a bar), so a holding is not sold and rebought just
    # because one session printed zero volume. Its close still marks a position (a stale
    # mark), and it still counts as a printed bar for the missing-bars delisting rule, so
    # a stale-quoted holding is kept until it trades again. An unknown (NaN) volume is
    # tradable; only library callers can pass it, as the platform's quality gate rejects it.
    no_trade = has_bar & (volume.to_numpy(dtype=float) <= 0)
    traded_bar = has_bar & ~no_trade
    missing_run = np.zeros(len(universe), dtype=np.int64)
    col = {s: j for j, s in enumerate(universe)}
    # From its delisting date on, a security is not offered to the strategy.
    delist_day = (
        pd.DatetimeIndex([delist_at[s][0] if s in delist_at else pd.NaT for s in universe])
        .as_unit("ns")
        .to_numpy()
    )
    delisted: set[str] = set()
    delist_counts = {
        "with_return": 0,
        "with_fallback_return": 0,
        "return_unknown": 0,
        "by_missing_bars": 0,
        "after_window": 0,
        "after_window_current": 0,
    }
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
    trades = _Rows(TRADE_COLUMNS, _TRADE_FLOATS)
    rejects: list[dict[str, Any]] = []
    pos_rows = _Rows(("date", "symbol", "quantity", "price"), frozenset({"price"}))
    decisions = _Rows(("date", "symbol", "weight"), frozenset({"weight"}))
    cum = {"commission": 0.0, "slippage": 0.0, "dividends": 0.0}
    # Held positions marked at a stale price: a zero-volume close, or the last close when
    # the session has no bar. Equity on stale marks is not realisable.
    stale_position_days = 0
    zero_volume_position_days = 0  # the subset marked at a zero-volume close
    # The largest share of equity on stale marks, and the share on the last session; None
    # where nothing is held with positive equity.
    stale_share: float | None = None
    final_share: float | None = None
    slip = config.slippage_bps / 1e4
    comm = config.commission_bps / 1e4

    def below_one_share(o: Order) -> bool:
        """A whole-share buy that is under one share as it reaches the fill, after any
        split restatement: for example the top-up of a holding a split left fractional
        (2.96 shares, target 3). It can never fill (#187)."""
        return (
            not config.fractional_shares and o.quantity > 0 and math.floor(o.quantity + 1e-9) == 0
        )

    def prorate_buys(k: int, px_row: np.ndarray, orders: list[Order]) -> bool:
        """Scale all buys by one factor when the cash left after sells cannot fund them,
        instead of filling whichever symbols sort first (see :func:`buy_scale`). A buy below
        one share takes no part: it can never fill, so it must not scale the others down
        (#187). True when there is no cash for any buy."""
        cmin = config.commission_min
        cash = ledger.cash
        # Without a minimum commission the exact rule is the earlier formula, and it is
        # computed exactly as before, so such runs stay bit-for-bit unchanged (#195).
        for o in orders:  # each sell's proceeds net of its own commission (#195)
            if o.quantity < 0 and math.isfinite(px_row[col[o.symbol]]):
                notional = min(-o.quantity, ledger.quantity(o.symbol)) * px_row[col[o.symbol]]
                notional *= 1 - slip
                if not cmin:
                    cash += notional * (1 - comm)
                elif notional:
                    cash += notional - max(cmin, notional * comm)
        buys = [o for o in orders if o.quantity > 0 and not below_one_share(o)]
        values = [
            o.quantity * px_row[col[o.symbol]] * (1 + slip)
            for o in buys
            if math.isfinite(px_row[col[o.symbol]]) and o.symbol not in delisted
        ]
        if not values:  # no buy can fill here (no price, or delisted)
            return False
        if cmin:
            need = sum(v + max(cmin, v * comm) for v in values)
        else:
            need = sum(v * (1 + comm) for v in values)
        # Cash up to a billionth of the need is rounding dust left by an earlier fill (#184):
        # no cash. The buys are neither scaled to dust quantities (whose record depended on
        # the dust's sign) nor filled with it: every one meets the shortfall as with zero.
        if cash <= need * 1e-9:
            return True
        if cash >= need:
            return False
        scale = buy_scale(values, comm, cmin, cash) if cmin else cash / need
        if scale is None:  # the minimum commissions alone exceed the cash
            return True
        for o in buys:
            o.quantity *= scale
            o.scaled = True
        return False

    def fill(k: int, px_row: np.ndarray, orders: list[Order]) -> float:
        d = dates[k]
        traded = 0.0
        no_cash = prorate_buys(k, px_row, orders)
        for o in sorted(orders, key=lambda o: (o.quantity > 0, o.symbol)):
            j = col[o.symbol]
            ref = px_row[j]
            if o.symbol in delisted and o.quantity > 0:  # a sell of nothing held is a no-op
                rejects.append(
                    {
                        "date": d,
                        "decision_date": o.decision_date,
                        "symbol": o.symbol,
                        "quantity": o.quantity,
                        "reason": "delisted",
                    }
                )
                continue
            if not math.isfinite(ref):
                rejects.append(
                    {
                        "date": d,
                        "decision_date": o.decision_date,
                        "symbol": o.symbol,
                        "quantity": o.quantity,
                        "reason": "no_volume" if no_trade[k, j] else "no_price",
                    }
                )
                continue
            qty = o.quantity
            if qty < 0:
                qty = max(qty, -ledger.quantity(o.symbol))
                if qty == 0:
                    continue
            buy = qty > 0
            # Under one share before any scaling: not a shortfall of cash, no lot to buy
            # (#187). A buy that scaling cut below one share is insufficient_cash (#184).
            if buy and not o.scaled and below_one_share(o):
                rejects.append(
                    {
                        "date": d,
                        "decision_date": o.decision_date,
                        "symbol": o.symbol,
                        "quantity": o.quantity,
                        "reason": "below_one_share",
                    }
                )
                continue
            if buy and not config.fractional_shares:
                qty = math.floor(qty + 1e-9)
            px = ref * (1 + slip) if buy else ref * (1 - slip)
            partial = o.scaled
            if buy:
                # Shrink buys to what the cash balance can fund, including commission.
                affordable = ledger.cash / (px * (1 + comm)) if px > 0 and not no_cash else 0.0
                if config.commission_min:
                    affordable = min(affordable, (ledger.cash - config.commission_min) / px)
                if not config.fractional_shares:
                    affordable = math.floor(affordable + 1e-9)
                if affordable < qty - 1e-9:
                    qty, partial = max(affordable, 0.0), True
                # A buy the cash cut to nothing is rejected, not dropped (#184): one scaled
                # below a whole share, or cut below the ledger's smallest quantity.
                if qty < LEDGER_EPS:
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
                d,
                o.decision_date,
                o.symbol,
                "buy" if qty > 0 else "sell",
                qty,
                ref,
                px,
                notional,
                commission,
                slip_cost,
                partial,
                None,
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
        for sym, (when, ret) in delist_at.items():
            if sym in delisted or d <= when:
                continue
            delisted.add(sym)
            j = col[sym]
            q = ledger.quantity(sym)
            if abs(q) < 1e-12 or not math.isfinite(last_px[j]):
                continue
            has_ret = math.isfinite(ret)
            value = last_px[j] * (1 + ret) if has_ret else last_px[j]
            if sym in fallback:
                note = f"delisting return unknown: assumed performance return applied ({ret:+.2%})"
            elif has_ret:
                note = f"delisting return applied ({ret:+.2%})"
            else:
                note = "delisting return unknown: liquidated at last close"
            ledger.delist(d, sym, max(value, 0.0), note=note)
            if sym in fallback:
                delist_counts["with_fallback_return"] += 1
            else:
                delist_counts["with_return" if has_ret else "return_unknown"] += 1
            trades.append(
                d,
                d,
                sym,
                "sell",
                -q,
                last_px[j],
                max(value, 0.0),
                abs(q) * max(value, 0.0),
                0.0,
                0.0,
                False,
                f"delisting: {note}",
            )
            traded += abs(q) * max(value, 0.0)
        for sym in list(ledger.positions):
            j = col[sym]
            if sym in delist_at:
                continue  # the master decides when (and at what value) it delists
            if missing_run[j] >= config.delist_after_missing and math.isfinite(last_px[j]):
                delist_counts["by_missing_bars"] += 1
                q = ledger.quantity(sym)
                ledger.trade(d, sym, -q, last_px[j], note="delisted: liquidated at last close")
                trades.append(
                    d,
                    d,
                    sym,
                    "sell",
                    -q,
                    last_px[j],
                    last_px[j],
                    abs(q) * last_px[j],
                    0.0,
                    0.0,
                    False,
                    "delisting",
                )
                traded += abs(q) * last_px[j]
        # 2/3. execute orders queued at the previous decision
        if pending:
            px_row = O[k] if config.execution == "next_open" else C[k]
            traded += fill(k, np.where(no_trade[k], np.nan, px_row), pending)
            pending = []
        # 4. mark to market
        row = C[k]
        last_px = np.where(np.isfinite(row), row, last_px)
        prices = {s: float(last_px[col[s]]) for s in ledger.positions}
        mv = ledger.market_value(prices)
        equity = ledger.cash + mv
        stale_held = [s for s in ledger.positions if not traded_bar[k, col[s]]]
        stale_position_days += len(stale_held)
        zero_volume_position_days += sum(bool(no_trade[k, col[s]]) for s in stale_held)
        final_share = None
        if ledger.positions and equity > 0:
            final_share = sum(ledger.positions[s] * prices[s] for s in stale_held) / equity
            stale_share = final_share if stale_share is None else max(stale_share, final_share)
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
                pos_rows.append(d, s, q, prices[s])
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
                tradable=pd.Series(has_bar[k] & ~(delist_day <= d.to_datetime64()), index=universe),
                holdings=holdings,
                raw_close=close_raw.iloc[: k + 1],
            )
            target = strategy.target_weights(view)
            if target is not None:
                target = _sanitize(target, view, config)
                for s, w in target.items():
                    decisions.append(d, s, float(w))
                pending = _orders(d, target, ledger, row, last_px, col, equity, config)
        if on_progress is not None and (step % 25 == 0 or step == n_steps - 1):
            on_progress((step + 1) / n_steps)

    # A held security the master delists on or after the last session never reaches its
    # delisting inside the window. If it traded on the last session its mark is a real
    # close (a study ending before its data does); if not (no bar, or a zero-volume one), it
    # is a stale mark frozen at an earlier close, which overstates a failing security. Only
    # the stale case is a concern.
    for sym, (when, _) in delist_at.items():
        if when >= dates[-1] and abs(ledger.quantity(sym)) > 1e-12:
            stale = not traded_bar[-1, col[sym]]
            delist_counts["after_window" if stale else "after_window_current"] += 1
    ledger.check_invariants()
    eq = pd.DataFrame(records).set_index("date")
    eq["gross_equity"] = eq["equity"] + eq["cum_commission"] + eq["cum_slippage"]
    eq["turnover"] = eq["traded_notional"] / eq["equity"].shift(1).fillna(config.initial_capital)
    # As built from row dicts before: no columns without trades, and a fill's missing
    # note is NaN when some trade has a note, None when none does.
    trades_df = trades.frame() if trades.n else pd.DataFrame()
    if len(trades_df) and trades_df["note"].notna().any():
        trades_df["note"] = trades_df["note"].where(trades_df["note"].notna(), np.nan)
    rej_df = pd.DataFrame(
        rejects, columns=["date", "decision_date", "symbol", "quantity", "reason"]
    )
    pos_df = pos_rows.frame()
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
    stale_undefined: dict[str, str] = {}
    if stale_share is None:
        stale_undefined["max_equity_share"] = "nothing was held on a day with positive equity"
    if final_share is None:
        stale_undefined["final_equity_share"] = (
            "nothing was held on the last session with positive equity"
        )
    result = BacktestResult(
        config=config,
        strategy=strategy.describe(),
        equity=eq,
        trades=trades_df,
        rejections=rej_df,
        positions=pos_df,
        weights_target=decisions.frame(),
        benchmark=bench_series,
        ledger=ledger,
        meta={
            "engine_version": ENGINE_VERSION,
            "universe": universe,
            "benchmark": bench,
            "sessions": n_steps,
            "not_listed_in_window": not_listed,
            "corporate_actions": action_counts,
            "delistings": delist_counts,
            # Universe bars in the window printed with zero volume: not tradable (#149).
            "zero_volume_bars": int(no_trade[start_pos:].sum()),
            # Orders rejected because their fill bar printed zero volume.
            "no_volume_rejections": sum(r["reason"] == "no_volume" for r in rejects),
            # Universe symbols with bars in the window, and those whose bars there all have
            # zero volume: the backtest could never trade them (a CSV uploaded with volume
            # 0, say).
            "symbols_with_bars": int(has_bar[start_pos:].any(axis=0).sum()),
            "never_traded": [
                s
                for j, s in enumerate(universe)
                if has_bar[start_pos:, j].any() and not traded_bar[start_pos:, j].any()
            ],
            # The cost of keeping stale-marked holdings (#149 review): equity marked at a
            # zero-volume close, or at the last close across a missing bar, is not realisable.
            "stale_marks": {
                "position_days": stale_position_days,
                "zero_volume_position_days": zero_volume_position_days,
                "max_equity_share": stale_share,
                "final_equity_share": final_share,
                "undefined": stale_undefined,
            },
            "instrument_master_unmatched": master_unmatched,
            "instrument_master": instruments is not None and len(instruments) > 0,
            "first_session": str(trade_dates[0].date()),
            "last_session": str(trade_dates[-1].date()),
        },
    )
    return result


def buy_scale(values: list[float], bps: float, cmin: float, cash: float) -> float | None:
    """The pro-rata factor (#195): the largest ``s`` <= 1 with

        sum_i ( s * v_i + max(cmin, s * v_i * bps) ) <= cash,

    where ``v_i`` is buy ``i``'s value at the fill price with slippage, ``bps`` the
    commission rate and ``cmin`` the minimum commission. None when no ``s`` > 0 is
    feasible, because the minimum commissions alone (``n * cmin``) take all the cash: the
    buys are then all rejected, which favours none of them.

    The left side is increasing and piecewise linear in ``s``. Buy ``i`` pays the minimum
    up to ``s = cmin / (v_i * bps)`` and the rate above it, so between consecutive
    breakpoints the total is ``s * (sum v + bps * sum_{rate} v) + cmin * n_{min}``, solved
    exactly. Rounding a scaled quantity down (whole shares) stays within the cash: a
    smaller quantity never raises ``max(cmin, .)``.
    """
    n = len(values)
    if cmin > 0 and n * cmin >= cash:
        return None
    if cmin <= 0 or bps <= 0:  # one segment: the rate everywhere, or the minimum everywhere
        return min(1.0, (cash - n * cmin) / (sum(values) * (1 + bps if cmin <= 0 else 1)))
    # The largest buy leaves the minimum first: its breakpoint cmin / (v * bps) is lowest.
    ordered = sorted(values, reverse=True)
    breaks = [cmin / (v * bps) for v in ordered]
    total = sum(values)
    at_rate = 0.0  # sum of v over the buys paying the rate in the current segment
    # Segments in increasing order: an earlier segment's formula undercounts the total past
    # its own end (some buys there pay more than the minimum), so its solution overshoots
    # that end. The first segment whose solution is within its end holds the answer.
    for i, hi in enumerate([*breaks, math.inf]):
        s = (cash - cmin * (n - i)) / (total + bps * at_rate)
        if s <= hi:
            s = min(s, 1.0)
            # Float rounding must not leave the total a hair over the cash.
            while s > 0 and sum(s * v + max(cmin, s * v * bps) for v in values) > cash:
                s = math.nextafter(s, 0.0)
            return s
        if i < n:
            at_rate += ordered[i]
    raise AssertionError("the total is continuous and increasing: some segment holds s")


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
