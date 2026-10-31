"""Nothing fills on a bar printed with zero volume (#149). Synthetic data only.

A vendor may keep printing a stale open and close with zero volume through a halt. Nobody
could trade at those prices, so an order meets such a bar as it meets a session without a
bar: it is rejected (``no_volume``), and the strategy is not offered the symbol that day.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
import pandas as pd
import pytest

from quantlab.backtest.engine import ENGINE_VERSION, BacktestConfig, run_backtest
from quantlab.data.identity import normalize_master
from quantlab.data.schema import normalize_actions
from quantlab.portfolio.ledger import replay
from quantlab.strategies.base import MarketView, Strategy, StrategyParams
from tests.conftest import make_bars

FREE = dict(commission_bps=0, slippage_bps=0, fractional_shares=True)
NONE = normalize_actions(None)
MODES = ("next_open", "next_close")


class Scripted(Strategy):
    """Holds B until ``switch_at`` (a session index; None: never), then A. Records what it
    was offered."""

    key: ClassVar[str] = "scripted"
    name: ClassVar[str] = "Scripted"
    description: ClassVar[str] = "test only"
    Params: ClassVar[type[StrategyParams]] = StrategyParams

    def __init__(self, dates: pd.DatetimeIndex, switch_at: int | None) -> None:
        super().__init__()
        self.switch = dates[switch_at] if switch_at is not None else dates[-1] + pd.Timedelta(1)
        self.offered: dict[pd.Timestamp, list[str]] = {}

    def target_weights(self, view: MarketView) -> pd.Series:
        self.offered[view.date] = view.universe
        return pd.Series({"A" if view.date >= self.switch else "B": 1.0})


def _zero_volume(bars: pd.DataFrame, sym: str, sessions: list[int], value=0.0) -> pd.DataFrame:
    d = pd.DatetimeIndex(sorted(bars["date"].unique()))
    out = bars.copy()
    out.loc[(out["symbol"] == sym) & out["date"].isin(d[sessions]), "volume"] = value
    return out


def _halt_case():
    # B is printed at its pre-halt price 10 with zero volume on sessions 4-7, then reopens
    # 30% lower at 7 (open and close) on session 8.
    b = [10.0] * 8 + [7.0] * 4
    bars = make_bars({"A": [10.0] * 12, "B": b}, opens={"B": b})
    bars = _zero_volume(bars, "B", [4, 5, 6, 7])
    return bars, pd.DatetimeIndex(sorted(bars["date"].unique()))


@pytest.mark.parametrize("execution", MODES)
def test_a_zero_volume_halt_cannot_be_sold_and_the_position_takes_the_reopen_move(execution):
    bars, d = _halt_case()
    strategy = Scripted(d, switch_at=4)  # decides to sell B at the first halt close
    cfg = BacktestConfig(rebalance="daily", initial_capital=1000, execution=execution, **FREE)
    res = run_backtest(bars, NONE, strategy, cfg)
    sells = res.trades[(res.trades["symbol"] == "B") & (res.trades["side"] == "sell")]
    # Before #149 the sell filled on session 5 at the stale 10, avoiding the loss.
    assert list(sells["date"]) == [d[8]] and sells["price"].iloc[0] == pytest.approx(7.0)
    rej = res.rejections[res.rejections["symbol"] == "B"]
    assert list(rej["date"]) == [d[5], d[6], d[7]] and set(rej["reason"]) == {"no_volume"}
    # 100 shares bought at 10; marked at the stale close through the halt, then sold at 7.
    assert res.equity["equity"].loc[d[7]] == pytest.approx(1000.0)  # a stale mark
    assert res.equity["equity"].iloc[-1] == pytest.approx(700.0)
    for k in (4, 5, 6, 7):  # not offered while it prints zero volume
        assert "B" not in strategy.offered[d[k]]
    assert "B" in strategy.offered[d[8]]
    assert res.meta["zero_volume_bars"] == 4 and res.meta["engine_version"] == ENGINE_VERSION
    res.ledger.check_invariants()
    rebuilt = replay(res.ledger.transactions)
    assert rebuilt.cash == pytest.approx(res.ledger.cash)
    assert rebuilt.positions == pytest.approx(res.ledger.positions)


@pytest.mark.parametrize("execution", MODES)
def test_a_single_zero_volume_day_rejects_the_fill_and_the_next_decision_fills(execution):
    # B trades flat at 10 except on session 2, a zero-volume print at 12 (a stale quote
    # that differs from the last trade). The buy decided on session 1 must not fill there.
    # B is not offered on session 2 either, as on a session without a bar, so the next
    # buy is decided on session 3 and fills on session 4.
    b = [10.0, 10.0, 12.0, 10.0, 10.0, 10.0]
    bars = _zero_volume(make_bars({"A": [10.0] * 6, "B": b}, opens={"B": b}), "B", [2])
    d = pd.DatetimeIndex(sorted(bars["date"].unique()))
    strategy = Scripted(d, switch_at=None)  # always B
    cfg = BacktestConfig(
        rebalance="daily", initial_capital=1000, execution=execution, start=d[1].date(), **FREE
    )
    res = run_backtest(bars, NONE, strategy, cfg)
    buys = res.trades[res.trades["symbol"] == "B"]
    assert d[2] not in set(buys["date"])
    rej = res.rejections[res.rejections["symbol"] == "B"]
    assert list(rej["date"]) == [d[2]] and list(rej["reason"]) == ["no_volume"]
    assert buys["date"].iloc[0] == d[4] and buys["price"].iloc[0] == pytest.approx(10.0)
    assert "B" not in strategy.offered[d[2]] and "B" in strategy.offered[d[3]]
    res.ledger.check_invariants()


def test_an_unknown_volume_is_tradable():
    # NaN volume is unknown, not zero: many datasets carry no volume at all.
    b = [10.0] * 6
    bars = _zero_volume(make_bars({"A": [10.0] * 6, "B": b}), "B", [1, 2], value=np.nan)
    d = pd.DatetimeIndex(sorted(bars["date"].unique()))
    cfg = BacktestConfig(rebalance="daily", initial_capital=1000, **FREE)
    res = run_backtest(bars, NONE, Scripted(d, switch_at=None), cfg)
    assert res.trades.loc[res.trades["symbol"] == "B", "date"].iloc[0] == d[1]
    assert res.rejections.empty and res.meta["zero_volume_bars"] == 0


def test_a_zero_volume_last_bar_is_a_stale_mark_after_the_window():
    # A security delisted after the window keeps its last mark. A zero-volume last close
    # is not a real close, so it is counted as stale (after_window), not current.
    bars, d = _halt_case()
    bars = _zero_volume(bars, "B", [11])
    master = normalize_master(
        pd.DataFrame(
            [
                {"instrument_id": "A", "name": "A", "listing_date": d[0]},
                {
                    "instrument_id": "B",
                    "name": "B",
                    "listing_date": d[0],
                    "delisting_date": d[-1] + pd.Timedelta(days=30),
                    "delisting_reason": "bankruptcy",
                },
            ]
        )
    )
    cfg = BacktestConfig(rebalance="quarterly", initial_capital=1000, **FREE)
    res = run_backtest(bars, NONE, Scripted(d, switch_at=None), cfg, instruments=master)
    assert res.meta["delistings"]["after_window"] == 1
    assert res.meta["delistings"]["after_window_current"] == 0
