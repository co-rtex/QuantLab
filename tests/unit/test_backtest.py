import numpy as np
import pandas as pd
import pytest

from quantlab.backtest.engine import BacktestConfig, BacktestError, rebalance_mask, run_backtest
from quantlab.data.adjust import total_returns
from quantlab.data.schema import normalize_actions
from quantlab.strategies.base import MarketView, Strategy, StrategyParams
from quantlab.strategies.library import REGISTRY, create_strategy
from tests.conftest import make_bars

FREE = dict(commission_bps=0, slippage_bps=0, fractional_shares=True)


def test_zero_cost_buy_and_hold_tracks_total_return_exactly(market):
    sym = "SYN002"
    cfg = BacktestConfig(universe=[sym], execution="next_close", **FREE)
    res = run_backtest(market.bars, market.actions, create_strategy("buy_and_hold"), cfg)
    eq = res.equity["equity"]
    tr = total_returns(market.bars[market.bars.symbol == sym], market.actions)[sym]
    # Invested at close of session 2 (decision at close of session 1, filled next close).
    idx = (1 + tr.loc[eq.index[1] :].iloc[1:]).cumprod()
    np.testing.assert_allclose(
        (eq.loc[idx.index] / eq.iloc[1]).to_numpy(), idx.to_numpy(), rtol=1e-10
    )


def test_deterministic_replay(market):
    cfg = BacktestConfig(benchmark="SYNIDX")
    a = run_backtest(
        market.bars, market.actions, create_strategy("momentum", {"lookback": 126}), cfg
    )
    b = run_backtest(
        market.bars, market.actions, create_strategy("momentum", {"lookback": 126}), cfg
    )
    assert a.fingerprint() == b.fingerprint()
    pd.testing.assert_frame_equal(a.trades, b.trades)


@pytest.mark.parametrize("key", sorted(REGISTRY))
def test_every_strategy_reconciles_and_respects_constraints(market, key):
    cfg = BacktestConfig(benchmark="SYNIDX", max_weight=0.2, rebalance="monthly")
    res = run_backtest(market.bars, market.actions, create_strategy(key), cfg)
    eq = res.equity
    # Portfolio value reconciles with positions and cash every day.
    if len(res.positions):
        mv = res.positions.groupby("date")["value"].sum().reindex(eq.index, fill_value=0.0)
        np.testing.assert_allclose(eq["cash"] + mv, eq["equity"], rtol=1e-10)
    assert (eq["cash"] >= -1e-6).all()
    assert (res.positions["quantity"] >= 0).all() if len(res.positions) else True
    tgt = res.weights_target
    if len(tgt):
        assert tgt["weight"].max() <= 0.2 + 1e-12
        assert (tgt.groupby("date")["weight"].sum() <= 1 + 1e-9).all()
    res.ledger.check_invariants()


def test_costs_reduce_wealth(market):
    s = create_strategy("equal_weight")
    free = run_backtest(market.bars, market.actions, s, BacktestConfig(**FREE))
    costly = run_backtest(
        market.bars,
        market.actions,
        s,
        BacktestConfig(commission_bps=10, slippage_bps=20, fractional_shares=True),
    )
    assert costly.equity["equity"].iloc[-1] < free.equity["equity"].iloc[-1]
    assert costly.equity["cum_commission"].iloc[-1] > 0
    # gross equity adds back costs paid
    g = costly.equity
    np.testing.assert_allclose(
        g["gross_equity"], g["equity"] + g["cum_commission"] + g["cum_slippage"]
    )


class Peeker(Strategy):
    """Records the last date it can see; any leak would show a date > decision date."""

    key = "peeker"
    name = "peeker"
    description = ""
    Params = StrategyParams
    seen: list[tuple[pd.Timestamp, pd.Timestamp, int]] = []

    def target_weights(self, view: MarketView):
        self.seen.append((view.date, view.close.index[-1], len(view.returns)))
        return pd.Series(1.0 / len(view.universe), index=view.universe)


def test_strategy_never_sees_future_bars(market):
    Peeker.seen = []
    run_backtest(market.bars, market.actions, Peeker(), BacktestConfig(rebalance="weekly"))
    assert Peeker.seen
    assert all(d == last for d, last, _ in Peeker.seen)


@pytest.mark.parametrize("key", ["momentum", "sma_crossover", "low_volatility", "min_variance"])
def test_signals_invariant_to_future_data(market, key):
    """Truncating the dataset after a decision date must not change that decision."""
    cut = pd.Timestamp("2021-06-30")
    cfg = BacktestConfig(rebalance="monthly")
    full = run_backtest(market.bars, market.actions, create_strategy(key), cfg)
    trunc = run_backtest(
        market.bars[market.bars.date <= cut + pd.Timedelta(days=1)],
        market.actions[market.actions.ex_date <= cut + pd.Timedelta(days=1)],
        create_strategy(key),
        cfg,
    )
    a = full.weights_target[full.weights_target.date <= cut].reset_index(drop=True)
    b = trunc.weights_target[trunc.weights_target.date <= cut].reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)


def test_fills_use_next_session_open_never_decision_close():
    bars = make_bars({"A": [10.0, 11.0, 12.0, 13.0]}, opens={"A": [10.0, 10.5, 11.5, 12.5]})
    cfg = BacktestConfig(universe=["A"], rebalance="daily", commission_bps=0, slippage_bps=0)
    res = run_backtest(bars, normalize_actions(None), create_strategy("buy_and_hold"), cfg)
    t = res.trades.iloc[0]
    assert t["decision_date"] < t["date"]
    assert t["price"] == 10.5  # open of the next session
    # Sized on the decision close (100,000 shares) but the gap-up open makes that
    # unaffordable, so the fill is reduced to what cash can fund and flagged partial.
    assert t["quantity"] == np.floor(1_000_000 / 10.5)
    assert t["partial"]


def test_slippage_and_commission_applied():
    bars = make_bars({"A": [100.0, 100.0, 100.0]})
    cfg = BacktestConfig(universe=["A"], commission_bps=10, slippage_bps=50, initial_capital=10_000)
    res = run_backtest(bars, normalize_actions(None), create_strategy("buy_and_hold"), cfg)
    t = res.trades.iloc[0]
    assert t["price"] == pytest.approx(100.5)
    assert t["commission"] == pytest.approx(t["notional"] * 0.001)
    assert t["partial"]  # sized at close without costs, so it is shrunk to fit cash
    assert res.equity["cash"].iloc[-1] >= 0


def test_split_between_decision_and_fill_restates_order():
    bars = make_bars({"A": [100.0, 50.0, 50.0]})
    d = bars["date"].unique()
    acts = normalize_actions(
        pd.DataFrame([{"symbol": "A", "ex_date": d[1], "kind": "split", "value": 2.0}])
    )
    cfg = BacktestConfig(universe=["A"], initial_capital=1000, **FREE)
    res = run_backtest(bars, acts, create_strategy("buy_and_hold"), cfg)
    assert res.trades.iloc[0]["quantity"] == pytest.approx(20)  # 10 pre-split -> 20 post
    assert res.equity["equity"].iloc[-1] == pytest.approx(1000)


def test_dividends_credited_and_value_preserved():
    bars = make_bars({"A": [100.0, 100.0, 98.0, 98.0]})
    d = bars["date"].unique()
    acts = normalize_actions(
        pd.DataFrame([{"symbol": "A", "ex_date": d[2], "kind": "dividend", "value": 2.0}])
    )
    cfg = BacktestConfig(universe=["A"], initial_capital=1000, **FREE)
    res = run_backtest(bars, acts, create_strategy("buy_and_hold"), cfg)
    assert res.equity["cum_dividends"].iloc[-1] == pytest.approx(20)
    assert res.equity["equity"].iloc[-1] == pytest.approx(1000)


def test_missing_bar_rejects_order_and_delisting_liquidates():
    bars = make_bars({"A": [10.0, 10.0, None, 12.0, 12.0], "B": [10.0, 10.0, 10.0, 11.0, None]})
    cfg = BacktestConfig(universe=["A", "B"], rebalance="daily", initial_capital=1000, **FREE)
    res = run_backtest(bars, normalize_actions(None), create_strategy("equal_weight"), cfg)
    assert "delisting" in set(res.trades.get("note", pd.Series(dtype=str)).dropna())
    final = res.equity.iloc[-1]
    assert final["positions"] == 1  # only A remains
    res.ledger.check_invariants()
    # A had no bar on session 3 so an order executing then is rejected
    if len(res.rejections):
        assert set(res.rejections["reason"]) <= {"no_price"}


def test_rebalance_mask_month_end():
    dates = pd.DatetimeIndex(["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-29", "2024-03-01"])
    assert rebalance_mask(dates, "monthly").tolist() == [True, True, False, True, True]


def test_errors():
    bars = make_bars({"A": [1.0, 2.0]})
    with pytest.raises(BacktestError):
        run_backtest(
            bars,
            normalize_actions(None),
            create_strategy("equal_weight"),
            BacktestConfig(universe=["ZZZ"]),
        )
    with pytest.raises(ValueError):
        BacktestConfig(start="2024-02-01", end="2024-01-01")
    with pytest.raises(ValueError):
        create_strategy("sma_crossover", {"fast": 100, "slow": 50})
