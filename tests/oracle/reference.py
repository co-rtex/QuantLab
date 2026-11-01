"""A reference backtester, written independently of ``quantlab.backtest.engine`` (#180).

It is deliberately plain and slow: per-instrument dictionaries and one loop over sessions.
It follows the rules as documented in ``docs/METHODOLOGY.md`` ("Data" and "Backtest
engine"), and it imports nothing from ``quantlab``. It takes ticker-level data and keys it
by instrument itself, from the symbol history, so ticker reuse and renames go through an
independent path too. The differential tests in ``tests/unit/test_engine_oracle.py``
compare it with the engine on generated universes (``tests.oracle.universes``).

Known gap (#190 review): the fill step (sell proceeds, pro-rata, affordability) follows
the engine's code closely rather than METHODOLOGY's text, so a defect shared there would
not show. One did: sells pay ``max(commission_min, notional x bps)`` as METHODOLOGY says,
but the pro-rata estimate of sell proceeds uses ``notional x bps`` (#195). #195 rewrites
this step from the text.

The rules, per session ``d`` of the window, in order:

1. **Open.** Each held instrument's corporate actions placed on ``d`` are applied in
   (ex-date, dividend before split) order: a dividend pays its value on the shares held at
   that point, and a split multiplies them (and divides the last close). An action is
   placed on the first bar of its own instrument dated on or after its ex-date; one before
   the instrument's first bar or after its last is not applied. Pending orders are
   restated in post-split shares.
2. **Delistings.** An instrument the master delists on ``w`` is closed on the first session
   after ``w``, at ``last close x (1 + return)``, the fallback return for a performance
   delisting with an unknown return when one is configured, or the last close, floored at
   0. Actions ex-dated after ``w`` are dropped. Any other held instrument that has printed
   no bar for ``delist_after_missing`` consecutive sessions (counted from the start of the
   window, including ``d``) is sold at its last close, without costs.
3. **Fills.** Orders decided at the previous rebalance fill at ``open[d]`` (``next_open``)
   or ``close[d]`` (``next_close``), with slippage. Nothing fills without a bar, or on a
   bar printed with volume <= 0 (an unknown, NaN, volume is tradable). When the cash after
   the sells cannot fund the buys, every buy is scaled by one factor; cash under a
   billionth of the need is rounding dust and counts as none. Sells run first, then buys,
   each group by instrument id. A buy is cut to what the cash can fund, including
   commission ``max(commission_min, notional x bps)``; one cut to nothing is rejected
   ``insufficient_cash`` (#184). With whole shares, a buy that is under one share as it
   reaches the fill (after any split restatement; a top-up of a holding a split left
   fractional) is rejected ``below_one_share`` and takes no part in the scaling (#187).
4. **Mark** at the close, or at the last close for an instrument without a bar. A position
   marked on a missing bar or a zero-volume bar is a stale mark.
5. **Decision** at the close of a rebalance session (the last session of each period and
   the window's first session, but never its last), once ``warmup`` sessions of data
   exist: the strategy sees data dated ``<= d`` only, and its weights become share targets
   at ``close[d]`` (or the last close) and the equity at the close.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

PERFORMANCE = {"bankruptcy", "liquidation", "noncompliance"}
EPS = 1e-9


@dataclass
class Bar:
    open: float
    close: float
    volume: float

    @property
    def tradable(self) -> bool:
        return not self.volume <= 0  # NaN volume is unknown, and tradable


@dataclass
class Order:
    decided: pd.Timestamp
    iid: str
    qty: float
    scaled: bool = False


@dataclass
class View:
    """What a reference strategy sees at the close of session ``k``."""

    k: int
    date: pd.Timestamp
    tradable: list[str]
    instruments: list[str]
    holdings: dict[str, float]
    trailing_return: Callable[[int, int], dict[str, float]]
    close: dict[str, float]  # each tradable instrument's close on session k


@dataclass
class Result:
    equity: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    rejections: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


Interval = tuple[str, str, pd.Timestamp, pd.Timestamp | None]  # ticker, instrument, from, to


def intervals(history: pd.DataFrame) -> list[Interval]:
    return [
        (
            str(r.symbol),
            str(r.instrument_id),
            pd.Timestamp(r.valid_from),
            None if pd.isna(r.valid_to) else pd.Timestamp(r.valid_to),
        )
        for r in history.itertuples()
    ]


def instrument_of(spans: list[Interval], ticker: str, day: pd.Timestamp) -> str:
    """The instrument holding ``ticker`` on ``day``: intervals are ``[valid_from, valid_to)``."""
    for symbol, iid, since, until in spans:
        if symbol == ticker and since <= day and (until is None or day < until):
            return iid
    raise LookupError(f"no instrument holds {ticker} on {day.date()}")


def _period(day: pd.Timestamp, freq: str) -> tuple[int, int]:
    if freq == "weekly":
        iso = day.isocalendar()
        return (iso[0], iso[1])
    if freq == "monthly":
        return (day.year, day.month)
    if freq == "quarterly":
        return (day.year, (day.month - 1) // 3)
    raise ValueError(freq)


def run_reference(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    history: pd.DataFrame,
    master: pd.DataFrame | None,
    cfg: dict[str, Any],
    strategy: Any,
) -> Result:
    end = pd.Timestamp(cfg["end"]) if cfg.get("end") else None
    spans = intervals(history)
    # --- data keyed by instrument -----------------------------------------------------
    by_inst: dict[str, dict[pd.Timestamp, Bar]] = {}
    for b in bars.itertuples():
        day = pd.Timestamp(b.date)
        if end is not None and day > end:
            continue
        iid = instrument_of(spans, b.symbol, day)
        by_inst.setdefault(iid, {})[day] = Bar(float(b.open), float(b.close), float(b.volume))
    ids = sorted(by_inst)
    sessions = sorted({day for series in by_inst.values() for day in series})
    pos_of = {day: i for i, day in enumerate(sessions)}

    delisting: dict[str, tuple[pd.Timestamp, float, str | None]] = {}
    if master is not None:
        for m in master.itertuples():
            if str(m.instrument_id) in by_inst and pd.notna(m.delisting_date):
                ret = float(m.delisting_return) if pd.notna(m.delisting_return) else math.nan
                reason = m.delisting_reason if isinstance(m.delisting_reason, str) else None
                delisting[str(m.instrument_id)] = (pd.Timestamp(m.delisting_date), ret, reason)

    # Placement: events[(iid, session index)] = [(ex_date, order, kind, value), ...]
    events: dict[tuple[str, int], list[tuple[pd.Timestamp, int, str, float]]] = {}
    placed = {"on_ex_date": 0, "applied_next_session": 0, "outside_history": 0}
    for a in actions.itertuples():
        ex = pd.Timestamp(a.ex_date)
        if end is not None and ex > end:
            continue
        iid = instrument_of(spans, a.symbol, ex)
        if iid not in by_inst:
            continue
        if iid in delisting and ex > delisting[iid][0]:
            continue  # the delisting return includes it
        bar_days = sorted(by_inst[iid])
        later = [day for day in bar_days if day >= ex]
        if ex < bar_days[0] or not later:
            placed["outside_history"] += 1
            continue
        placed["on_ex_date" if later[0] == ex else "applied_next_session"] += 1
        order = 0 if a.kind == "dividend" else 1
        events.setdefault((iid, pos_of[later[0]]), []).append((ex, order, a.kind, float(a.value)))
    for evs in events.values():
        evs.sort(key=lambda e: (e[0], e[1]))

    def bar(iid: str, k: int) -> Bar | None:
        return by_inst[iid].get(sessions[k])

    # Gross total return of each bar against the instrument's previous bar.
    gross: dict[tuple[str, int], float] = {}
    for iid in ids:
        prev = None
        for k, day in enumerate(sessions):
            b = by_inst[iid].get(day)
            if b is None:
                continue
            if prev is not None:
                shares, cash = 1.0, 0.0
                for _, _, kind, value in events.get((iid, k), []):
                    if kind == "dividend":
                        cash += shares * value
                    else:
                        shares *= value
                gross[(iid, k)] = (shares * b.close + cash) / prev
            prev = b.close

    def trailing(k: int) -> Callable[[int, int], dict[str, float]]:
        def ret(lookback: int, skip: int = 0) -> dict[str, float]:
            out = {}
            for iid in ids:
                lo, hi = k - lookback, k - skip
                if lo < 0 or bar(iid, lo) is None or bar(iid, hi) is None:
                    out[iid] = math.nan
                    continue
                g = 1.0
                for j in range(lo + 1, hi + 1):
                    g *= gross.get((iid, j), 1.0)
                out[iid] = g - 1.0
            return out

        return ret

    # --- simulation -------------------------------------------------------------------
    start = pd.Timestamp(cfg["start"]) if cfg.get("start") else None
    first = 0 if start is None else next(i for i, day in enumerate(sessions) if day >= start)
    window = range(first, len(sessions))
    freq = cfg["rebalance"]
    rebalance = set()
    for i in window:
        last_of_period = i == len(sessions) - 1 or (
            freq == "daily" or _period(sessions[i], freq) != _period(sessions[i + 1], freq)
        )
        if i == first or last_of_period:
            rebalance.add(i)

    slip, comm = cfg["slippage_bps"] / 1e4, cfg["commission_bps"] / 1e4
    cmin, whole = cfg["commission_min"], not cfg["fractional_shares"]

    def fee(value: float) -> float:
        """A trade's commission: the larger of the minimum and the rate on its value."""
        return max(cmin, value * comm) if value else 0.0

    res = Result()
    cash = float(cfg["initial_capital"])
    qty: dict[str, float] = {}
    last: dict[str, float] = {}
    missing = dict.fromkeys(ids, 0)
    delisted: set[str] = set()
    pending: list[Order] = []
    total = {"commission": 0.0, "slippage": 0.0, "dividends": 0.0}
    counts = dict.fromkeys(
        (
            "with_return",
            "with_fallback_return",
            "return_unknown",
            "by_missing_bars",
            "after_window",
            "after_window_current",
        ),
        0,
    )
    stale_days = zero_days = 0
    max_share: float | None = None
    final_share: float | None = None

    def trade_row(day, decided, iid, q, ref, px, commission, slip_cost, partial, note):
        res.trades.append(
            {
                "date": day,
                "decision_date": decided,
                "symbol": iid,
                "side": "buy" if q > 0 else "sell",
                "quantity": q,
                "reference_price": ref,
                "price": px,
                "notional": abs(q) * px,
                "commission": commission,
                "slippage_cost": slip_cost,
                "partial": partial,
                "note": note,
            }
        )

    def reject(day, o: Order, reason: str) -> None:
        res.rejections.append(
            {
                "date": day,
                "decision_date": o.decided,
                "symbol": o.iid,
                "quantity": o.qty,
                "reason": reason,
            }
        )

    def set_qty(iid: str, q: float) -> None:
        if abs(q) < EPS:
            qty.pop(iid, None)
        else:
            qty[iid] = q

    for k in window:
        day = sessions[k]
        traded = 0.0
        # 1. corporate actions at the open
        for iid in list(qty):
            for _, _, kind, value in events.get((iid, k), []):
                if kind == "dividend":
                    cash += qty[iid] * value
                    total["dividends"] += qty[iid] * value
                else:
                    qty[iid] *= value
                    last[iid] /= value
        for o in pending:
            for _, _, kind, value in events.get((o.iid, k), []):
                if kind == "split":
                    o.qty *= value
        # 2. delistings
        for iid in sorted(delisting):
            when, ret, reason = delisting[iid]
            if iid in delisted or day <= when:
                continue
            delisted.add(iid)
            if iid not in qty:
                continue
            if not math.isnan(ret):
                value, kind, note = last[iid] * (1 + ret), "with_return", "return"
            elif cfg.get("performance_delisting_return") is not None and reason in PERFORMANCE:
                value = last[iid] * (1 + cfg["performance_delisting_return"])
                kind, note = "with_fallback_return", "fallback"
            else:
                value, kind, note = last[iid], "return_unknown", "unknown"
            value = max(value, 0.0)
            q = qty.pop(iid)
            cash += q * value
            counts[kind] += 1
            trade_row(day, day, iid, -q, last[iid], value, 0.0, 0.0, False, f"delisting:{note}")
            traded += q * value
        for iid in ids:
            missing[iid] = 0 if bar(iid, k) is not None else missing[iid] + 1
        for iid in list(qty):
            if iid in delisting or missing[iid] < cfg["delist_after_missing"]:
                continue
            q = qty.pop(iid)
            cash += q * last[iid]
            counts["by_missing_bars"] += 1
            trade_row(day, day, iid, -q, last[iid], last[iid], 0.0, 0.0, False, "delisting:bars")
            traded += q * last[iid]
        # 3. fills
        if pending:
            ref: dict[str, float] = {}
            why: dict[str, str] = {}
            for o in pending:
                b = bar(o.iid, k)
                if b is None:
                    why[o.iid] = "no_price"
                elif not b.tradable:
                    why[o.iid] = "no_volume"
                else:
                    ref[o.iid] = b.open if cfg["execution"] == "next_open" else b.close

            # From METHODOLOGY step 4 (#195): a sell's proceeds are its value at the fill
            # price with slippage, less its commission.
            after_sells = cash
            for o in pending:
                if o.qty < 0 and o.iid in ref:
                    value = min(-o.qty, qty.get(o.iid, 0.0)) * ref[o.iid] * (1 - slip)
                    after_sells += value - fee(value)
            # With whole shares, a buy that reaches the fill (after any split restatement)
            # under one share has no lot to buy: it is rejected, and it neither counts toward
            # the need nor is scaled (#187).
            no_lot = {o.iid for o in pending if whole and 0 < o.qty < 1 - 1e-9}
            real = [o for o in pending if o.qty > 0 and o.iid not in no_lot]
            worth = [
                o.qty * ref[o.iid] * (1 + slip)
                for o in real
                if o.iid in ref and o.iid not in delisted
            ]

            def cost(factor: float, worth: list[float] = worth) -> float:
                return sum(factor * v + fee(factor * v) for v in worth)

            # Cash of at most a billionth of the need is rounding dust: there is no cash for
            # any buy, so none is scaled and none is bought (#184). Nor is there when the
            # minimum commissions alone take it all: every buy is then rejected.
            broke = bool(worth) and (
                after_sells <= cost(1.0) * 1e-9
                or (cost(1.0) > after_sells and cmin > 0 and len(worth) * cmin >= after_sells)
            )
            if worth and not broke and cost(1.0) > after_sells:
                # The largest common factor whose scaled buys fit the cash, by bisection
                # (the engine solves it in closed form).
                lo, hi = 0.0, 1.0
                for _ in range(200):
                    mid = (lo + hi) / 2
                    if mid in (lo, hi):
                        break
                    lo, hi = (mid, hi) if cost(mid) <= after_sells else (lo, mid)
                for o in real:
                    o.qty *= lo
                    o.scaled = True
            for o in sorted(pending, key=lambda o: (o.qty > 0, o.iid)):
                if o.qty > 0 and o.iid in delisted:
                    reject(day, o, "delisted")
                    continue
                if o.iid not in ref:
                    reject(day, o, why[o.iid])
                    continue
                r = ref[o.iid]
                held = qty.get(o.iid, 0.0)
                partial = o.scaled
                if o.qty < 0:
                    q = max(o.qty, -held)
                    if q == 0:
                        continue
                    px = r * (1 - slip)
                else:
                    if o.iid in no_lot:
                        reject(day, o, "below_one_share")
                        continue
                    q = math.floor(o.qty + 1e-9) if whole else o.qty
                    if broke:
                        reject(day, o, "insufficient_cash")
                        continue
                    px = r * (1 + slip)
                    # The most the cash can buy: q px (1 + bps) <= cash and q px + cmin <= cash.
                    can = min(cash / (px * (1 + comm)), (cash - cmin) / px) if px > 0 else 0.0
                    if whole:
                        can = math.floor(can + 1e-9)
                    if can < q - 1e-9:
                        q, partial = max(can, 0.0), True
                    # The cash may not go more than 1e-9 dollars below zero. A buy that float
                    # rounding leaves a hair over is made smaller until it fits (#196).
                    # One share at a time, or a fractional step that doubles from one unit in
                    # the last place.
                    shave, tries = math.ulp(q), 0
                    while q > 0 and cash - (q * px + fee(q * px)) < -EPS and tries < 128:
                        if whole:
                            q -= 1
                        else:
                            q, shave = max(q - shave, 0.0), 2 * shave
                        partial, tries = True, tries + 1
                    # Nothing left to buy (scaled under one share, or no affordable amount
                    # the ledger can book): a shortfall of cash (#184).
                    if q < EPS:
                        reject(day, o, "insufficient_cash")
                        continue
                notional = abs(q) * px
                commission = max(cmin, notional * comm) if notional else 0.0
                if abs(q) < EPS:
                    reject(day, o, "InvalidOrder")
                    continue
                # As the cash is updated below, cash - (cost), not (cash - value) - fee:
                # at 1e8 and more the two round differently by more than EPS.
                if cash - (q * px + commission) < -EPS:
                    # Only a sell can get here: its commission is more than its proceeds and
                    # the cash can cover (#196).
                    reject(day, o, "commission_exceeds_proceeds" if q < 0 else "InsufficientCash")
                    continue
                cash -= q * px + commission
                set_qty(o.iid, held + q)
                total["commission"] += commission
                total["slippage"] += abs(q) * abs(px - r)
                traded += notional
                trade_row(
                    day, o.decided, o.iid, q, r, px, commission, abs(q) * abs(px - r), partial, None
                )
            pending = []
        # 4. mark
        for iid in ids:
            b = bar(iid, k)
            if b is not None:
                last[iid] = b.close
        value = sum(q * last[iid] for iid, q in qty.items())
        equity = cash + value
        stale = [iid for iid in qty if bar(iid, k) is None or not bar(iid, k).tradable]
        stale_days += len(stale)
        zero_days += sum(1 for iid in stale if bar(iid, k) is not None)
        final_share = None
        if qty and equity > 0:
            final_share = sum(qty[iid] * last[iid] for iid in stale) / equity
            max_share = final_share if max_share is None else max(max_share, final_share)
        res.equity.append(
            {
                "date": day,
                "equity": equity,
                "cash": cash,
                "market_value": value,
                "positions": len(qty),
                "traded_notional": traded,
                "cum_commission": total["commission"],
                "cum_slippage": total["slippage"],
                "cum_dividends": total["dividends"],
            }
        )
        for iid, q in qty.items():
            res.positions.append({"date": day, "symbol": iid, "quantity": q, "price": last[iid]})
        # 5. decision
        if k in rebalance and k + 1 < len(sessions) and k + 1 >= strategy.warmup:
            tradable = [
                iid
                for iid in ids
                if bar(iid, k) is not None and not (iid in delisting and delisting[iid][0] <= day)
            ]
            holdings = {iid: q * last[iid] / equity for iid, q in qty.items()} if equity > 0 else {}
            closes = {iid: bar(iid, k).close for iid in tradable}
            view = View(k, day, tradable, ids, holdings, trailing(k), closes)
            target = strategy.decide(view)
            if target is not None:
                weights = {
                    iid: float(w)
                    for iid, w in target.items()
                    if iid in tradable and math.isfinite(float(w)) and float(w) > 0
                }
                weights = {iid: min(w, cfg["max_weight"]) for iid, w in weights.items()}
                if sum(weights.values()) > cfg["max_gross_exposure"]:
                    scale = cfg["max_gross_exposure"] / sum(weights.values())
                    weights = {iid: w * scale for iid, w in weights.items()}
                for iid in sorted(weights):
                    res.decisions.append({"date": day, "symbol": iid, "weight": weights[iid]})
                for iid in sorted(set(weights) | set(qty)):
                    b = bar(iid, k)
                    px = b.close if b is not None else last.get(iid, math.nan)
                    if not px > 0:
                        continue
                    want = weights.get(iid, 0.0) * equity / px
                    if whole:
                        want = math.floor(want + 1e-9)
                    have = qty.get(iid, 0.0)
                    delta = want - have
                    # No order for a change under 1e-9 shares, or under a trillionth of the
                    # larger of the target and the holding: float noise, not a trade (#215).
                    scale = max(1.0, abs(want), abs(have))
                    if abs(delta) < 1e-9 or abs(delta) < scale * 1e-12:
                        continue
                    if abs(delta) * px < cfg["min_trade_value"]:
                        continue
                    pending.append(Order(day, iid, delta))

    last_day = sessions[-1]
    for iid, (when, _, _) in delisting.items():
        if when >= last_day and iid in qty:
            b = bar(iid, len(sessions) - 1)
            current = b is not None and b.tradable
            counts["after_window_current" if current else "after_window"] += 1
    in_window = [sessions[i] for i in window]
    res.meta = {
        "sessions": len(in_window),
        "first_session": str(in_window[0].date()),
        "last_session": str(in_window[-1].date()),
        "corporate_actions": placed,
        "delistings": counts,
        "zero_volume_bars": sum(
            1
            for iid in ids
            for day in in_window
            if day in by_inst[iid] and not by_inst[iid][day].tradable
        ),
        "no_volume_rejections": sum(r["reason"] == "no_volume" for r in res.rejections),
        "symbols_with_bars": sum(1 for iid in ids if any(day in by_inst[iid] for day in in_window)),
        "never_traded": [
            iid
            for iid in ids
            if any(day in by_inst[iid] for day in in_window)
            and not any(by_inst[iid][day].tradable for day in in_window if day in by_inst[iid])
        ],
        "stale_marks": {
            "position_days": stale_days,
            "zero_volume_position_days": zero_days,
            "max_equity_share": max_share,
            "final_equity_share": final_share,
        },
    }
    res.meta["final_cash"] = cash
    res.meta["final_positions"] = dict(qty)
    return res
