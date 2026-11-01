"""Run the engine and the reference on one scenario and list every difference (#180).

Values match when ``|a - b| <= 1e-9 * max(|a|, |b|, floor)``. The floor is the initial
capital for money columns, so a cash balance of about 0 is compared at the run's scale,
and 1 for share counts, prices and weights. Rows are compared after sorting by their keys:
the engine records same-session trades and positions in the order its dicts hold them,
which carries no meaning.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from tests.oracle import strategies as S
from tests.oracle.reference import Result, run_reference
from tests.oracle.universes import Scenario, scenario

RTOL = 1e-9
STRATEGIES = ("equal_weight", "momentum", "random", "buy_and_hold", "price_weight")
MONEY = {
    "equity",
    "cash",
    "market_value",
    "traded_notional",
    "cum_commission",
    "cum_slippage",
    "cum_dividends",
    "notional",
    "commission",
    "slippage_cost",
}


def strategies_for(name: str, seed: int) -> tuple[Any, Any]:
    from quantlab.strategies.library import create_strategy

    if name == "equal_weight":
        return create_strategy("equal_weight"), S.EqualWeightRef()
    if name == "buy_and_hold":
        return create_strategy("buy_and_hold"), S.BuyAndHoldRef()
    if name == "momentum":
        skip, top = seed % 3, 1 + seed % 3
        return (
            create_strategy("momentum", {"lookback": 20, "skip": skip, "top_n": top}),
            S.MomentumRef(20, skip, top),
        )
    if name == "random":
        return S.random_engine_strategy(seed), S.RandomRef(seed)
    if name == "price_weight":
        return S.price_weight_engine_strategy(), S.PriceWeightRef()
    raise ValueError(name)


def keyed(sc: Scenario) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The scenario's data keyed by instrument, as the platform hands it to the engine."""
    from quantlab.data.identity import key_by_instrument, normalize_history

    return key_by_instrument(sc.bars, sc.actions, normalize_history(sc.history))


def run_engine(
    sc: Scenario,
    cfg: dict[str, Any],
    strategy: Any,
    data: tuple[pd.DataFrame, pd.DataFrame] | None = None,
) -> Any:
    from quantlab.backtest.engine import BacktestConfig, run_backtest

    bars, actions = data if data is not None else keyed(sc)
    return run_backtest(bars, actions, strategy, BacktestConfig(**cfg), instruments=sc.master)


def _close(a: Any, b: Any, floor: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str | bool) or isinstance(b, str | bool):
        return a == b
    a, b = float(a), float(b)
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return abs(a - b) <= RTOL * max(abs(a), abs(b), floor)


def _rows(frame: pd.DataFrame | list[dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    df = pd.DataFrame(frame)
    if not len(df):
        return []
    return df.sort_values(keys, kind="stable").to_dict("records")


def _compare_rows(what, eng, ref, keys, cols, cap, out) -> None:
    if len(eng) != len(ref):
        out.append(f"{what}: {len(eng)} engine rows vs {len(ref)} reference rows")
    for i, (e, r) in enumerate(zip(eng, ref, strict=False)):
        for c in cols:
            ev, rv = e.get(c), r.get(c)
            if isinstance(ev, pd.Timestamp) or isinstance(rv, pd.Timestamp):
                same = pd.Timestamp(ev) == pd.Timestamp(rv)
            else:
                same = _close(ev, rv, cap if c in MONEY else 1.0)
            if not same:
                where = ", ".join(f"{k}={e.get(k)}" for k in keys)
                out.append(f"{what}[{i}] ({where}) {c}: engine {ev!r} vs reference {rv!r}")
                return  # the first difference in a table; later rows usually follow from it


def default_runs(seed: int) -> list[tuple[str, str]]:
    """Both execution modes with two strategies per seed, rotating through all of them."""
    n = len(STRATEGIES)
    pair = (STRATEGIES[seed % n], STRATEGIES[(seed + 1) % n])
    return [(ex, st) for ex in ("next_open", "next_close") for st in pair]


def compare(seed: int, runs: list[tuple[str, str]] | None = None) -> list[str]:
    """Every difference between engine and reference on ``seed``, for each run
    (execution, strategy); the scenario is generated and keyed once."""
    sc = scenario(seed)
    data = keyed(sc)
    out = []
    for execution, strategy in runs or default_runs(seed):
        cfg = {**sc.cfg, "execution": execution}
        engine_strategy, ref_strategy = strategies_for(strategy, seed)
        eng = run_engine(sc, cfg, engine_strategy, data)
        ref = run_reference(sc.bars, sc.actions, sc.history, sc.master, cfg, ref_strategy)
        out += [
            f"seed {seed} {execution} {strategy}: {d}"
            for d in diff(eng, ref, cfg["initial_capital"])
        ]
    return out


def diff(eng: Any, ref: Result, cap: float) -> list[str]:
    out: list[str] = []
    eq = eng.equity.reset_index()
    cols = [
        "equity",
        "cash",
        "market_value",
        "positions",
        "traded_notional",
        "cum_commission",
        "cum_slippage",
        "cum_dividends",
    ]
    _compare_rows("equity", eq.to_dict("records"), ref.equity, ["date"], ["date", *cols], cap, out)
    trades = eng.trades.copy()
    if len(trades):
        trades["note"] = trades["note"].map(
            lambda n: "delisting" if isinstance(n, str) and n.startswith("delisting") else None
        )
    ref_trades = [{**t, "note": "delisting" if t["note"] else None} for t in ref.trades]
    tcols = [
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
    ]
    keys = ["date", "symbol", "side"]
    _compare_rows("trades", _rows(trades, keys), _rows(ref_trades, keys), keys, tcols, cap, out)
    keys = ["date", "symbol", "reason"]
    _compare_rows(
        "rejections",
        _rows(eng.rejections, keys),
        _rows(ref.rejections, keys),
        keys,
        ["date", "decision_date", "symbol", "quantity", "reason"],
        cap,
        out,
    )
    keys = ["date", "symbol"]
    pos = eng.positions[["date", "symbol", "quantity", "price"]] if len(eng.positions) else []
    _compare_rows(
        "positions",
        _rows(pos, keys),
        _rows(ref.positions, keys),
        keys,
        ["date", "symbol", "quantity", "price"],
        cap,
        out,
    )
    _compare_rows(
        "decisions",
        _rows(eng.weights_target, keys),
        _rows(ref.decisions, keys),
        keys,
        ["date", "symbol", "weight"],
        cap,
        out,
    )
    if not _close(eng.ledger.cash, ref.meta["final_cash"], cap):
        out.append(f"ledger cash {eng.ledger.cash!r} vs reference {ref.meta['final_cash']!r}")
    held = {s: q for s, q in eng.ledger.positions.items()}
    if set(held) != set(ref.meta["final_positions"]) or not all(
        _close(held[s], ref.meta["final_positions"][s], 1.0) for s in held
    ):
        out.append(f"ledger positions {held} vs reference {ref.meta['final_positions']}")
    m = eng.meta
    for key in (
        "sessions",
        "first_session",
        "last_session",
        "corporate_actions",
        "delistings",
        "zero_volume_bars",
        "no_volume_rejections",
        "symbols_with_bars",
        "never_traded",
    ):
        if m[key] != ref.meta[key]:
            out.append(f"meta {key}: engine {m[key]!r} vs reference {ref.meta[key]!r}")
    for key, rv in ref.meta["stale_marks"].items():
        if not _close(m["stale_marks"][key], rv, 1.0):
            out.append(
                f"meta stale_marks.{key}: engine {m['stale_marks'][key]!r} vs reference {rv!r}"
            )
    return out
