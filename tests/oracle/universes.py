"""Small adversarial universes for the differential tests (#180). Synthetic data only.

Each seed gives 3-8 instruments over 30-120 weekday sessions, at the ticker level (as a
vendor would supply them), with a symbol history and, for most seeds, an instrument
master. The events are combined at random:

* late listings, early ends and suspensions (runs of sessions without a bar);
* zero-volume bars (a stale repeat of the last close) and unknown (NaN) volume;
* splits and reverse splits, cash dividends, a dividend and a split on one ex-date, and
  ex-dates on a weekend or inside a suspension (applied at the next bar);
* actions before an instrument's first bar or after its last (not applied);
* delistings with a known return (including a worthless -100%), an unknown return, a
  performance reason (for the fallback return), bars printed after the delisting date,
  and a delisting after the last session;
* renames, and a ticker reused by a later instrument after the first one ends;
* configurations that vary execution, costs, minimum commissions, whole or fractional
  shares, small capital (so cash runs short and buys are scaled), large capital (1e8 to
  1e12, where cash rounding is dollars, #196), weight and exposure caps, a minimum trade
  value, the missing-bars threshold, the window and the frequency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

REASONS = ["merger", "bankruptcy", "liquidation", "noncompliance", "voluntary", "other"]
DAY = pd.Timedelta(days=1)


@dataclass
class Scenario:
    seed: int
    bars: pd.DataFrame  # ticker-level, as supplied
    actions: pd.DataFrame
    history: pd.DataFrame
    master: pd.DataFrame | None
    cfg: dict[str, Any]
    synthetic: bool = True
    notes: list[str] = field(default_factory=list)


@dataclass
class _Inst:
    iid: str
    rows: list[int]
    ticker: str
    intervals: list[tuple[str, pd.Timestamp, pd.Timestamp | None]] = field(default_factory=list)
    weekend: pd.Timestamp | None = None  # a Saturday split ex-date, placed on Monday's bar


def scenario(seed: int) -> Scenario:
    rng = np.random.default_rng(seed)
    n = int(rng.integers(30, 121))
    days = pd.bdate_range("2022-01-03", periods=n)
    insts = [_shape(rng, i, n) for i in range(int(rng.integers(3, 9)))]
    notes: list[str] = []
    origin = pd.Timestamp("2021-01-01")

    # Ticker reuse: a later instrument takes the ticker of one that ended earlier. The
    # first holder's interval ends 11 days after its last bar, leaving room for an action
    # dated after its history; the second lists 12 sessions after the first's last bar.
    if rng.random() < 0.5:
        x, y = insts[0], insts[1]
        cut = int(rng.integers(x.rows[0] + 5, max(x.rows[0] + 6, n // 2)))
        x_rows = [r for r in x.rows if r <= cut]
        y_rows = [r for r in y.rows if r >= cut + 12] or list(range(cut + 12, n))
        if len(x_rows) >= 2 and len(y_rows) >= 2:
            x.rows, y.rows = x_rows, y_rows
            x_end = days[x.rows[-1]] + 11 * DAY
            x.intervals = [(x.ticker, origin, x_end)]
            y.ticker = x.ticker
            y.intervals = [(x.ticker, x_end, None)]
            notes.append(f"{y.iid} reuses {x.iid}'s ticker {x.ticker} from {x_end.date()}")
    for inst in insts:
        if inst.intervals:
            continue
        if len(inst.rows) >= 10 and rng.random() < 0.25:
            at = days[inst.rows[len(inst.rows) // 2]]
            new = inst.ticker + "N"
            inst.intervals = [(inst.ticker, origin, at), (new, at, None)]
            notes.append(f"{inst.iid} renamed {inst.ticker} -> {new} on {at.date()}")
        else:
            inst.intervals = [(inst.ticker, origin, None)]

    bar_rows, action_rows = [], []
    for inst in insts:
        b, a = _series(rng, inst, days)
        bar_rows += b
        action_rows += a
    bars = pd.DataFrame(bar_rows)
    actions = pd.DataFrame(action_rows, columns=["symbol", "ex_date", "kind", "value"])
    history = pd.DataFrame(
        [
            {"instrument_id": inst.iid, "symbol": t, "valid_from": f, "valid_to": to}
            for inst in insts
            for t, f, to in inst.intervals
        ]
    )
    history["valid_to"] = pd.to_datetime(history["valid_to"])
    master = _master(rng, insts, days) if rng.random() < 0.65 else None
    return Scenario(seed, bars, actions, history, master, _config(rng, days), notes=notes)


def _shape(rng: np.random.Generator, i: int, n: int) -> _Inst:
    lo = 0 if rng.random() < 0.6 else int(rng.integers(1, n // 3))
    hi = n - 1 if rng.random() < 0.55 else int(rng.integers(max(lo + 8, 2 * n // 3), n))
    hi = min(hi, n - 1)
    rows = list(range(lo, hi + 1))
    for _ in range(int(rng.integers(0, 3))):  # suspensions
        g = int(rng.integers(lo + 1, max(lo + 2, hi - 1)))
        gone = set(range(g, g + int(rng.integers(1, 9))))
        rows = [r for r in rows if r not in gone or r in (rows[0], rows[-1])]
    return _Inst(f"ID{i}", rows, f"TK{i}")


def _ticker_on(inst: _Inst, day: pd.Timestamp) -> str | None:
    for t, f, to in inst.intervals:
        if f <= day and (to is None or day < to):
            return t
    return None


def _series(
    rng: np.random.Generator, inst: _Inst, days: pd.DatetimeIndex
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    bars: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    price = float(rng.uniform(5, 200))
    prev_close = price
    zero_run = 0
    weekend_at = len(inst.rows) // 2 if rng.random() < 0.25 else len(inst.rows)
    for p, r in enumerate(inst.rows):
        day = days[r]
        factor = 1.0  # raw price change from this bar's corporate actions
        if p >= 1:
            prev_day = days[inst.rows[p - 1]]
            span = (day - prev_day).days
            if p >= weekend_at and inst.weekend is None and day.weekday() == 0 and span >= 3:
                # A Saturday ex-date: the master may delist on it too (see _master), so the
                # delisting and the split both fall on Monday's session.
                inst.weekend = day - 2 * DAY
                ratio = float(rng.choice([2.0, 3.0, 0.5, 0.1]))
                actions.append(_act(inst, inst.weekend, "split", ratio))
                factor /= ratio
            elif rng.random() < 0.07:  # a split, sometimes with a same-day dividend
                ratio = float(rng.choice([2.0, 3.0, 1.5, 4.0, 0.5, 0.2, 0.1]))
                ex = day if rng.random() < 0.6 else prev_day + int(rng.integers(1, span + 1)) * DAY
                if rng.random() < 0.3:
                    actions.append(_act(inst, ex, "dividend", 0.01 * prev_close))
                actions.append(_act(inst, ex, "split", ratio))
                factor /= ratio
                if span > 1 and rng.random() < 0.5:  # a second action in the same gap
                    ex2 = prev_day + int(rng.integers(1, span + 1)) * DAY
                    actions.append(_act(inst, ex2, "dividend", 0.005 * prev_close / ratio))
            elif rng.random() < 0.06:
                ex = day if rng.random() < 0.6 else prev_day + int(rng.integers(1, span + 1)) * DAY
                actions.append(
                    _act(inst, ex, "dividend", float(rng.uniform(0.002, 0.03)) * prev_close)
                )
        if zero_run == 0 and rng.random() < 0.04:
            zero_run = int(rng.integers(1, 6))
        if zero_run > 0 and p > 0:  # a vendor repeating the last close through a halt
            zero_run -= 1
            open_, close, volume = prev_close * factor, prev_close * factor, 0.0
        else:
            close = prev_close * factor * float(np.exp(rng.normal(0.0005, 0.025)))
            if rng.random() < 0.03:
                close *= float(rng.choice([0.8, 1.15]))
            open_ = prev_close * factor * float(np.exp(rng.normal(0, 0.01)))
            volume = float("nan") if rng.random() < 0.02 else float(rng.integers(1_000, 1_000_000))
        ticker = _ticker_on(inst, day)
        assert ticker is not None
        bars.append(
            {
                "symbol": ticker,
                "date": day,
                "open": open_,
                "high": max(open_, close),
                "low": min(open_, close),
                "close": close,
                "volume": volume,
            }
        )
        prev_close = close
    first, last = days[inst.rows[0]], days[inst.rows[-1]]
    start, stop = inst.intervals[0][1], inst.intervals[-1][2]
    if rng.random() < 0.15 and first - 10 * DAY >= start:  # before the history: not applied
        actions.append(_act(inst, first - int(rng.integers(1, 11)) * DAY, "dividend", 0.5))
    if rng.random() < 0.15 and (stop is None or last + 10 * DAY < stop):  # after it
        actions.append(_act(inst, last + int(rng.integers(1, 11)) * DAY, "split", 2.0))
    return bars, actions


def _act(inst: _Inst, ex: pd.Timestamp, kind: str, value: float) -> dict[str, Any]:
    ticker = _ticker_on(inst, ex)
    assert ticker is not None, (inst.iid, ex)
    return {"symbol": ticker, "ex_date": ex, "kind": kind, "value": value}


def _master(rng: np.random.Generator, insts: list[_Inst], days: pd.DatetimeIndex) -> pd.DataFrame:
    rows = []
    n = len(days)
    for inst in insts:
        row: dict[str, Any] = {
            "instrument_id": inst.iid,
            "name": f"Synthetic {inst.iid}",
            "listing_date": days[inst.rows[0]] if rng.random() < 0.5 else pd.NaT,
            "delisting_date": pd.NaT,
            "delisting_reason": None,
            "delisting_return": np.nan,
        }
        last = inst.rows[-1]
        u = rng.random()
        when = None
        if inst.weekend is not None and u < 0.8:  # delisted on a split's Saturday ex-date
            when = inst.weekend
        elif last < n - 1 and u < 0.75:  # ended early: on its last bar or a few days later
            when = days[last] + int(rng.integers(0, 6)) * DAY
        elif last == n - 1 and u < 0.12:  # after the window
            when = days[-1] + int(rng.integers(0, 4)) * DAY
        elif u < 0.2 and len(inst.rows) > 12:  # bars printed after the delisting date
            when = days[inst.rows[len(inst.rows) // 2]]
        if when is not None:
            row["delisting_date"] = when
            row["delisting_reason"] = str(rng.choice(REASONS))
            v = rng.random()
            row["delisting_return"] = (
                -1.0 if v < 0.1 else float(rng.uniform(-0.9, 0.2)) if v < 0.55 else np.nan
            )
        rows.append(row)
    return pd.DataFrame(rows)


def _config(rng: np.random.Generator, days: pd.DatetimeIndex) -> dict[str, Any]:
    n = len(days)
    small = rng.random() < 0.4
    cfg: dict[str, Any] = {
        "start": None,
        "end": None,
        "initial_capital": float(rng.uniform(3_000, 30_000)) if small else 1_000_000.0,
        "rebalance": str(
            rng.choice(["daily", "weekly", "monthly", "quarterly"], p=[0.35, 0.35, 0.2, 0.1])
        ),
        "commission_bps": float(rng.choice([0.0, 1.0, 10.0, 25.0])),
        "commission_min": float(rng.choice([0.0, 0.0, 1.0, 5.0])),
        "slippage_bps": float(rng.choice([0.0, 5.0, 30.0])),
        "fractional_shares": bool(rng.random() < 0.35),
        "max_weight": 1.0 if rng.random() < 0.6 else float(rng.uniform(0.25, 0.6)),
        "max_gross_exposure": 1.0 if rng.random() < 0.6 else float(rng.uniform(0.5, 0.95)),
        "min_trade_value": 0.0 if rng.random() < 0.7 else float(rng.uniform(100, 2_000)),
        "delist_after_missing": int(rng.integers(1, 8)),
        "performance_delisting_return": -0.3 if rng.random() < 0.4 else None,
    }
    if rng.random() < 0.3:
        cfg["start"] = (days[int(rng.integers(1, n // 3))] - int(rng.integers(0, 2)) * DAY).date()
    if rng.random() < 0.2:
        cfg["end"] = days[int(rng.integers(2 * n // 3, n - 1))].date()
    # A quarter of the seeds run a large account, 1e8 to 1e12, where cash rounding is dollars
    # rather than fractions of a cent (#196). Drawn last, so the other draws are unchanged.
    if rng.random() < 0.25:
        cfg["initial_capital"] = float(10 ** rng.uniform(8, 12))
    return cfg
