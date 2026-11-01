"""The strategy never sees a corporate action before its ex-date (#164). Synthetic data only.

The RC assessment's mutation test moved ``PointInTimeAdjuster.scale_as_of`` one bar ahead
(``_rev[as_of_pos + 2]``), and the whole suite passed while a strategy saw a 2:1 split on
the session before it happened. These tests go through the engine's ``MarketView``, the
path a strategy actually reads.
"""

from __future__ import annotations

from typing import ClassVar

import pandas as pd
import pytest

from quantlab.backtest.engine import BacktestConfig, run_backtest
from quantlab.data.adjust import adjusted_close
from quantlab.data.schema import normalize_actions
from quantlab.strategies.base import MarketView, Strategy, StrategyParams
from tests.conftest import make_bars

SPLIT_AT = 5  # 2:1 split ex-dated on session 5: the raw close halves from 100 to 50


class Spy(Strategy):
    """Holds A and records the adjusted and raw closes it is shown each session."""

    key: ClassVar[str] = "spy"
    name: ClassVar[str] = "Spy"
    description: ClassVar[str] = "test only"
    Params: ClassVar[type[StrategyParams]] = StrategyParams

    def __init__(self) -> None:
        super().__init__()
        self.seen: dict[pd.Timestamp, tuple[pd.DataFrame, pd.DataFrame]] = {}

    def target_weights(self, view: MarketView) -> pd.Series:
        assert view.raw_close is not None
        self.seen[view.date] = (view.close.copy(), view.raw_close.copy())
        return pd.Series({"A": 1.0})


def _case(kind: str = "split", value: float = 2.0):
    a = [100.0] * SPLIT_AT + [50.0] * 5 if kind == "split" else [100.0] * 10
    bars = make_bars({"A": a, "B": [20.0] * 10})
    d = pd.DatetimeIndex(sorted(bars["date"].unique()))
    actions = normalize_actions(
        pd.DataFrame([{"symbol": "A", "ex_date": d[SPLIT_AT], "kind": kind, "value": value}])
    )
    return bars, actions, d


def _run(bars, actions) -> Spy:
    spy = Spy()
    cfg = BacktestConfig(rebalance="daily", initial_capital=1000, fractional_shares=True)
    run_backtest(bars, actions, spy, cfg)
    return spy


def test_a_split_is_invisible_to_the_strategy_before_its_ex_date():
    bars, actions, d = _case()
    spy = _run(bars, actions)
    for k in range(SPLIT_AT):  # every session before the ex-date
        close, raw = spy.seen[d[k]]
        # No action has happened yet: adjusted equals raw on every row the strategy sees.
        pd.testing.assert_series_equal(close["A"], raw["A"], check_names=False)
        assert close["A"].iloc[-1] == pytest.approx(100.0)
    close, raw = spy.seen[d[SPLIT_AT]]  # on the ex-date the history is restated
    assert (close["A"].iloc[:SPLIT_AT] == 50.0).all() and close["A"].iloc[-1] == 50.0


@pytest.mark.parametrize(("kind", "value"), [("split", 2.0), ("split", 0.5), ("dividend", 5.0)])
def test_every_view_equals_the_as_of_adjustment_on_that_day(kind, value):
    # The strategy's view on day t equals adjusted_close as of t (events on or before t
    # only), up to the view's normalisation to the as-of scale.
    bars, actions, d = _case(kind, value)
    if kind == "split" and value == 0.5:  # a 1:2 reverse split doubles the raw close
        bars.loc[(bars["symbol"] == "A") & (bars["date"] >= d[SPLIT_AT]), "close"] = 200.0
        bars.loc[(bars["symbol"] == "A") & (bars["date"] >= d[SPLIT_AT]), "open"] = 200.0
    spy = _run(bars, actions)
    for t, (close, _) in spy.seen.items():
        want = adjusted_close(bars, actions, as_of=t)["A"]
        pd.testing.assert_series_equal(
            close["A"], want.loc[close.index], check_names=False, rtol=1e-12
        )
