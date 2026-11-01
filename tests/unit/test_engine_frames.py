"""The engine's result frames keep their dtypes and note semantics (#163). Synthetic data.

#163 changed how the engine stores result rows while it simulates. The fingerprint covers
only equity and each trade's symbol, quantity and price, so these pin what it does not:
column order, dtypes, a whole-share run's int64 quantities, and the ``note`` column's
NaN (some trade has a note) versus None (no trade does).
"""

from __future__ import annotations

from typing import ClassVar

import pandas as pd

from quantlab.backtest.engine import TRADE_COLUMNS, BacktestConfig, run_backtest
from quantlab.data.schema import normalize_actions
from quantlab.strategies.base import MarketView, Strategy, StrategyParams
from quantlab.strategies.library import create_strategy
from tests.conftest import make_bars

NONE = normalize_actions(None)
CFG = BacktestConfig(
    rebalance="monthly",
    initial_capital=1000,
    commission_bps=0,
    slippage_bps=0,
    delist_after_missing=3,
)
FLOAT = "float64"
TRADES = {
    "date": "datetime64[ns]",
    "decision_date": "datetime64[ns]",
    "symbol": "str",
    "side": "str",
    "reference_price": FLOAT,
    "price": FLOAT,
    "notional": FLOAT,
    "commission": FLOAT,
    "slippage_cost": FLOAT,
    "partial": "bool",
}
POSITIONS = {
    "date": "datetime64[ns]",
    "symbol": "str",
    "quantity": FLOAT,
    "price": FLOAT,
    "value": FLOAT,
    "weight": FLOAT,
}
WEIGHTS = {"date": "datetime64[ns]", "symbol": "str", "weight": FLOAT}


def _dtypes(frame: pd.DataFrame) -> dict[str, str]:
    return {c: str(t) for c, t in frame.dtypes.items()}


def _run(bars: pd.DataFrame, strategy: Strategy | None = None):
    return run_backtest(bars, NONE, strategy or create_strategy("equal_weight"), CFG)


def test_a_run_with_a_note_has_nan_for_the_fills_and_str_dtype():
    # B stops printing bars and is liquidated by the missing-bars rule, with a note.
    bars = make_bars({"A": [10.0] * 12, "B": [10.0, 10, 8, 6, 5] + [None] * 7})
    res = _run(bars)
    t = res.trades
    assert list(t.columns) == list(TRADE_COLUMNS)
    assert _dtypes(t) == {**TRADES, "quantity": FLOAT, "note": "str"}
    assert t["note"].isna().sum() == len(t) - 1 and list(t["note"].dropna()) == ["delisting"]
    assert all(isinstance(v, float) for v in t.loc[t["note"].isna(), "note"])  # NaN, not None
    assert _dtypes(res.positions) == POSITIONS and _dtypes(res.weights_target) == WEIGHTS


def test_a_run_without_notes_has_none_and_whole_shares_stay_int64():
    res = _run(make_bars({"A": [10.0] * 12, "B": [10.0] * 12}))
    t = res.trades
    assert list(t.columns) == list(TRADE_COLUMNS)
    assert _dtypes(t) == {**TRADES, "quantity": "int64", "note": "object"}
    assert t["note"].tolist() == [None] * len(t)


class Cash(Strategy):
    key: ClassVar[str] = "cash"
    name: ClassVar[str] = "Cash"
    description: ClassVar[str] = "test only"
    Params: ClassVar[type[StrategyParams]] = StrategyParams

    def target_weights(self, view: MarketView) -> pd.Series:
        return pd.Series(dtype=float)


def test_an_empty_run_has_the_old_empty_frames():
    res = _run(make_bars({"A": [10.0] * 6}), Cash())
    assert res.trades.empty and list(res.trades.columns) == []  # as from an empty row list
    assert list(res.positions.columns) == ["date", "symbol", "quantity", "price"]
    assert set(_dtypes(res.positions).values()) == {"object"}
    assert list(res.weights_target.columns) == ["date", "symbol", "weight"]
