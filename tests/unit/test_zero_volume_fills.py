"""Nothing fills on a bar printed with zero volume (#149). Synthetic data only.

A vendor may keep printing a stale open and close with zero volume through a halt. Nobody
could trade at those prices, so an order meets such a bar as it meets a session without a
bar: it is rejected (``no_volume``). The strategy is still offered the symbol (it has a
bar), so a holding is kept through zero-volume sessions rather than sold and rebought.
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
from quantlab.strategies.library import create_strategy
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


class Cash(Strategy):
    key: ClassVar[str] = "cash"
    name: ClassVar[str] = "Cash"
    description: ClassVar[str] = "test only"
    Params: ClassVar[type[StrategyParams]] = StrategyParams

    def target_weights(self, view: MarketView) -> pd.Series:
        return pd.Series(dtype=float)


def _dates(bars: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(sorted(bars["date"].unique()))


def _zero_volume(bars: pd.DataFrame, sym: str, sessions: list[int], value=0.0) -> pd.DataFrame:
    out = bars.copy()
    out.loc[(out["symbol"] == sym) & out["date"].isin(_dates(bars)[sessions]), "volume"] = value
    return out


def _halt_case():
    # B is printed at its pre-halt price 10 with zero volume on sessions 4-7, then reopens
    # 30% lower at 7 (open and close) on session 8.
    b = [10.0] * 8 + [7.0] * 4
    bars = make_bars({"A": [10.0] * 12, "B": b}, opens={"B": b})
    bars = _zero_volume(bars, "B", [4, 5, 6, 7])
    return bars, _dates(bars)


def _daily(execution: str = "next_open", **kw) -> BacktestConfig:
    return BacktestConfig(
        rebalance="daily", initial_capital=1000, execution=execution, **FREE, **kw
    )


@pytest.mark.parametrize("execution", MODES)
def test_a_zero_volume_halt_cannot_be_sold_and_the_position_takes_the_reopen_move(execution):
    bars, d = _halt_case()
    strategy = Scripted(d, switch_at=4)  # decides to sell B at the first halt close
    res = run_backtest(bars, NONE, strategy, _daily(execution))
    sells = res.trades[(res.trades["symbol"] == "B") & (res.trades["side"] == "sell")]
    # Before #149 the sell filled on session 5 at the stale 10, avoiding the loss.
    assert list(sells["date"]) == [d[8]] and sells["price"].iloc[0] == pytest.approx(7.0)
    rej = res.rejections[res.rejections["symbol"] == "B"]
    assert list(rej["date"]) == [d[5], d[6], d[7]] and set(rej["reason"]) == {"no_volume"}
    # 100 shares bought at 10; marked at the stale close through the halt, then sold at 7.
    assert res.equity["equity"].loc[d[7]] == pytest.approx(1000.0)  # a stale mark
    assert res.equity["equity"].iloc[-1] == pytest.approx(700.0)
    assert all("B" in strategy.offered[d[k]] for k in (4, 5, 6, 7))  # it has a bar
    assert res.meta["zero_volume_bars"] == 4 and res.meta["engine_version"] == ENGINE_VERSION
    # The cost is visible: 100 shares of B, the whole portfolio, on 4 stale marks.
    stale = res.meta["stale_marks"]
    assert stale["position_days"] == 4
    assert stale["max_equity_share"] == pytest.approx(1.0) and stale["undefined"] == {}
    res.ledger.check_invariants()
    rebuilt = replay(res.ledger.transactions)
    assert rebuilt.cash == pytest.approx(res.ledger.cash)
    assert rebuilt.positions == pytest.approx(res.ledger.positions)


@pytest.mark.parametrize("execution", MODES)
def test_a_single_zero_volume_day_rejects_the_fill_and_the_next_decision_fills(execution):
    # B trades flat at 10 except on session 2, a zero-volume print at 12 (a stale quote
    # that differs from the last trade). The buy decided on session 1 must not fill there;
    # the one decided on session 2 fills on session 3.
    b = [10.0, 10.0, 12.0, 10.0, 10.0, 10.0]
    bars = _zero_volume(make_bars({"A": [10.0] * 6, "B": b}, opens={"B": b}), "B", [2])
    d = _dates(bars)
    res = run_backtest(bars, NONE, Scripted(d, None), _daily(execution, start=d[1].date()))
    buys = res.trades[res.trades["symbol"] == "B"]
    rej = res.rejections[res.rejections["symbol"] == "B"]
    assert list(rej["date"]) == [d[2]] and list(rej["reason"]) == ["no_volume"]
    assert buys["date"].iloc[0] == d[3] and buys["price"].iloc[0] == pytest.approx(10.0)
    # Held from session 3, after the zero-volume day: no stale mark, a defined share of 0.
    assert res.meta["stale_marks"]["position_days"] == 0
    assert res.meta["stale_marks"]["max_equity_share"] == 0.0
    res.ledger.check_invariants()


# ------------------------------------------- #150 review B1: holdings are kept through
@pytest.mark.parametrize("execution", MODES)
def test_a_holding_is_kept_through_a_zero_volume_halt(execution):
    # A strategy that always wants all of B neither sells at the reopen nor rebuys later.
    bars, d = _halt_case()
    res = run_backtest(bars, NONE, Scripted(d, None), _daily(execution))
    b = res.trades[res.trades["symbol"] == "B"]
    assert list(b["side"]) == ["buy"] and b["date"].iloc[0] == d[1]
    assert res.rejections.empty
    assert res.equity["equity"].iloc[-1] == pytest.approx(700.0)  # 100 shares at 7


def test_equal_weight_keeps_a_name_with_one_zero_volume_rebalance_day():
    # The review's case: X prints zero volume on the 2024-01-31 rebalance close only. It
    # must not be sold on 02-01 and rebought on 03-01.
    n = 45
    bars = make_bars({"X": [10.0] * n, "Y": [20.0] * n})
    d = _dates(bars)
    zero = _zero_volume(bars, "X", [int(d.get_loc(pd.Timestamp("2024-01-31")))])
    cfg = BacktestConfig(rebalance="monthly", initial_capital=1000, **FREE)
    res = run_backtest(zero, NONE, create_strategy("equal_weight"), cfg)
    ref = run_backtest(bars, NONE, create_strategy("equal_weight"), cfg)
    assert len(res.trades[res.trades["symbol"] == "X"]) == 1  # the initial buy only
    pd.testing.assert_series_equal(res.equity["equity"], ref.equity["equity"])


def test_buy_and_hold_never_buys_a_name_rejected_on_its_first_fill():
    # Buy and hold invests once. A name whose first fill session prints zero volume is
    # rejected there and never bought, as with a missing bar on that session (documented).
    bars = _zero_volume(make_bars({"A": [10.0] * 8, "B": [10.0] * 8}), "B", [1])
    cfg = BacktestConfig(rebalance="monthly", initial_capital=1000, **FREE)
    res = run_backtest(bars, NONE, create_strategy("buy_and_hold"), cfg)
    assert set(res.trades["symbol"]) == {"A"}
    assert list(res.rejections["reason"]) == ["no_volume"]


# --------------------------------------------- stale marks, never-traded names (N1, N2)
def test_a_zombie_name_without_a_master_is_held_on_stale_marks_and_reported():
    # B trades on sessions 0-3, then prints zero volume to the end; there is no instrument
    # master. Zero-volume bars are printed bars, so the missing-bars rule never sells it.
    bars = _zero_volume(make_bars({"A": [10.0] * 12, "B": [10.0] * 12}), "B", list(range(4, 12)))
    res = run_backtest(bars, NONE, Scripted(_dates(bars), None), _daily())
    assert res.meta["delistings"]["by_missing_bars"] == 0
    assert res.ledger.quantity("B") == pytest.approx(100.0)
    stale = res.meta["stale_marks"]
    assert stale["position_days"] == 8 and stale["zero_volume_position_days"] == 8
    assert stale["max_equity_share"] == pytest.approx(1.0)
    # NB5: still on a stale mark at the end of the window, so visible at the end too.
    assert stale["final_equity_share"] == pytest.approx(1.0)
    assert res.meta["never_traded"] == []  # it traded in the window
    c = _checks(res.meta)["zero_volume"]
    assert c["status"] == "warning" and "100% at the end" in c["finding"]


def test_a_missing_bar_under_a_holding_is_a_stale_mark_too():
    # NB4: stale exposure from missing bars warns even with no zero-volume bar at all.
    b = [10.0, 10.0, 10.0, None, None, 10.0, 10.0, 10.0]
    bars = make_bars({"A": [10.0] * 8, "B": b})
    res = run_backtest(bars, NONE, Scripted(_dates(bars), None), _daily())
    stale = res.meta["stale_marks"]
    assert stale["position_days"] == 2 and stale["zero_volume_position_days"] == 0
    assert stale["final_equity_share"] == 0.0  # it trades again before the end
    assert res.meta["zero_volume_bars"] == 0
    c = _checks(res.meta)["zero_volume"]
    assert c["status"] == "warning" and "missing bars) on 2 position-days" in c["finding"]


def test_a_universe_name_that_never_traded_is_named():
    # A CSV uploaded with volume 0 for a symbol: it can never be traded, and says so.
    bars = make_bars({"A": [10.0] * 6, "Z": [10.0] * 6})
    bars.loc[bars["symbol"] == "Z", "volume"] = 0.0
    cfg = BacktestConfig(rebalance="monthly", initial_capital=1000, **FREE)
    res = run_backtest(bars, NONE, create_strategy("equal_weight"), cfg)
    assert res.meta["never_traded"] == ["Z"]
    assert "Z" not in set(res.trades["symbol"])


def test_the_stale_mark_share_is_undefined_when_nothing_is_held():
    bars, _ = _halt_case()
    stale = run_backtest(bars, NONE, Cash(), _daily()).meta["stale_marks"]
    assert stale["position_days"] == 0 and stale["max_equity_share"] is None
    assert stale["final_equity_share"] is None
    assert "nothing was held" in stale["undefined"]["max_equity_share"]
    assert "last session" in stale["undefined"]["final_equity_share"]


# ------------------------------------------------------------------- other choices
def test_an_unknown_volume_is_tradable():
    # NaN volume is unknown, not zero (library callers only: the quality gate rejects it).
    bars = _zero_volume(make_bars({"A": [10.0] * 6, "B": [10.0] * 6}), "B", [1, 2], value=np.nan)
    res = run_backtest(bars, NONE, Scripted(_dates(bars), None), _daily())
    assert res.trades.loc[res.trades["symbol"] == "B", "date"].iloc[0] == _dates(bars)[1]
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
    res = run_backtest(bars, NONE, Scripted(d, None), cfg, instruments=master)
    assert res.meta["delistings"]["after_window"] == 1
    assert res.meta["delistings"]["after_window_current"] == 0
    c = _checks(res.meta)["delisting_exits"]
    assert "a zero-volume bar, on the last session" in c["finding"]


# ------------------------------------------------------ integrity report (N1, N3)
def _checks(meta: dict) -> dict[str, dict]:
    from quantlab.research.integrity import build_integrity_report

    ev = {
        "experiment": {"config": {}},
        "dataset": {"provenance": {"provider": "csv"}, "quality_status": "pass"},
        "analysis": {"meta": meta},
    }
    return {c["key"]: c for c in build_integrity_report(ev)["checks"]}


def test_one_never_traded_name_is_a_warning_even_when_targeted():
    # A target that cannot fill stays in cash, which is modelled correctly, and equal
    # weight targets every name it is offered: one dead name is a warning, not a failure.
    bars = make_bars({"A": [10.0] * 6, "Z": [10.0] * 6})
    bars.loc[bars["symbol"] == "Z", "volume"] = 0.0
    cfg = BacktestConfig(rebalance="monthly", initial_capital=1000, **FREE)
    meta = run_backtest(bars, NONE, create_strategy("equal_weight"), cfg).meta
    assert meta["symbols_with_bars"] == 2 and meta["no_volume_rejections"] == 1
    c = _checks(meta)["zero_volume"]
    assert c["status"] == "warning"
    assert "1 of 2 universe symbols" in c["finding"] and "never be traded (Z)" in c["finding"]
    assert "must not be recorded as volume 0" in c["finding"]


def test_the_integrity_report_fails_when_nothing_in_the_universe_ever_traded():
    bars = make_bars({"Y": [10.0] * 6, "Z": [10.0] * 6})
    bars["volume"] = 0.0  # a whole CSV uploaded with volume 0
    cfg = BacktestConfig(rebalance="monthly", initial_capital=1000, **FREE)
    res = run_backtest(bars, NONE, create_strategy("equal_weight"), cfg)
    assert res.trades.empty and res.meta["never_traded"] == ["Y", "Z"]
    c = _checks(res.meta)["zero_volume"]
    assert c["status"] == "failed" and c["finding"].startswith("Nothing in the universe")


def test_the_integrity_report_warns_when_the_run_was_affected_and_states_the_exposure():
    bars, d = _halt_case()
    c = _checks(run_backtest(bars, NONE, Scripted(d, None), _daily()).meta)["zero_volume"]
    assert c["status"] == "warning"
    assert "marked at a zero-volume close on 4 position-days" in c["finding"]
    assert "at most 100% of equity on one day and 0% at the end" in c["finding"]
    assert "not realisable" in c["finding"]


def test_zero_volume_bars_that_did_not_affect_the_run_are_verified():
    # NB3: B prints zero volume, but nothing was held or ordered on those bars.
    bars, _ = _halt_case()
    meta = run_backtest(bars, NONE, Cash(), _daily()).meta
    c = _checks(meta)["zero_volume"]
    assert c["status"] == "verified" and "4 universe bars printed zero volume" in c["finding"]


def test_the_integrity_report_verifies_data_without_zero_volume_and_skips_old_results():
    bars = make_bars({"A": [10.0] * 6, "B": [10.0] * 6})
    meta = run_backtest(bars, NONE, Scripted(_dates(bars), None), _daily()).meta
    c = _checks(meta)["zero_volume"]
    assert c["status"] == "verified" and c["finding"].startswith("No universe bar")
    assert "zero_volume" not in _checks({"universe": ["A"]})  # before #149: no such check
