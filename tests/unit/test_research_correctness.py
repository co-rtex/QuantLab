"""End-to-end research-correctness checks with independent references (issue #35).

Where a test asserts a number, it is computed by hand in the test from share counts and
prices, not from library helpers. The look-ahead, corrected-history and row-order tests
are self-comparisons instead (two runs that must agree), which is the right design for
an invariance. Synthetic data only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quantlab.analytics.performance import compute_performance
from quantlab.backtest.engine import BacktestConfig, run_backtest
from quantlab.data.schema import normalize_actions
from quantlab.data.store import content_hash
from quantlab.strategies.base import MarketView, Strategy, StrategyParams
from quantlab.strategies.library import REGISTRY, create_strategy
from tests.conftest import make_bars

FREE = dict(commission_bps=0, slippage_bps=0, fractional_shares=True)
NONE = normalize_actions(None)


def _sessions(bars: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(sorted(bars["date"].unique()))


# ------------------------------------------------------------------- look-ahead
@pytest.mark.parametrize("key", sorted(REGISTRY))
def test_close_execution_also_ignores_perturbed_future_data(market, key):
    """Same rewrite-the-future check as test_backtest, for ``execution="next_close"``.

    With close execution an order decided at close k fills at close k+1, so a one-bar leak
    would read exactly the bar that fills it; it must not affect anything up to the cut.
    """
    cfg = BacktestConfig(rebalance="weekly", execution="next_close", benchmark="SYNIDX")
    a = run_backtest(market.bars, market.actions, create_strategy(key), cfg)
    decided = a.weights_target["date"].unique()
    cut = pd.Timestamp(decided[len(decided) // 2]) if len(decided) else pd.Timestamp("2021-03-26")
    bars = market.bars.copy()
    future = bars["date"] > cut
    shock = pd.Series(
        np.random.default_rng(5).uniform(0.5, 2.0, bars["symbol"].nunique()),
        bars["symbol"].unique(),
    )
    for c in ("open", "high", "low", "close"):
        bars.loc[future, c] *= bars.loc[future, "symbol"].map(shock)
    b = run_backtest(bars, market.actions, create_strategy(key), cfg)
    for name in ("weights_target", "trades"):
        x, y = (getattr(r, name) for r in (a, b))
        pd.testing.assert_frame_equal(
            x[x["date"] <= cut].reset_index(drop=True), y[y["date"] <= cut].reset_index(drop=True)
        )
    assert (a.equity["equity"][:cut] == b.equity["equity"][:cut]).all()


# --------------------------------------------------------------- execution timing
@pytest.mark.parametrize(("execution", "fill"), [("next_open", 10.5), ("next_close", 11.0)])
def test_fill_price_is_the_next_sessions_open_or_close(execution, fill):
    bars = make_bars({"A": [10.0, 11.0, 12.0]}, opens={"A": [9.0, 10.5, 11.5]})
    cfg = BacktestConfig(universe=["A"], execution=execution, initial_capital=1000, **FREE)
    res = run_backtest(bars, NONE, create_strategy("buy_and_hold"), cfg)
    t = res.trades.iloc[0]
    d = _sessions(bars)
    assert t["decision_date"] == d[0] and t["date"] == d[1]
    assert t["price"] == fill
    # Sized on the decision close (10.0): 100 shares wanted; cash allows 1000 / fill.
    assert t["quantity"] == pytest.approx(min(100.0, 1000 / fill))


# --------------------------------------------------- dividends and the benchmark
def _dividend_case() -> tuple[pd.DataFrame, pd.DataFrame]:
    # 100 -> ex-dividend $2 (price 98) -> +10% (107.8)
    bars = make_bars({"A": [100.0, 100.0, 98.0, 107.8]})
    d = _sessions(bars)
    acts = normalize_actions(
        pd.DataFrame([{"symbol": "A", "ex_date": d[2], "kind": "dividend", "value": 2.0}])
    )
    return bars, acts


def test_ledger_keeps_dividends_in_cash_while_total_return_reinvests():
    bars, acts = _dividend_case()
    cfg = BacktestConfig(universe=["A"], benchmark="A", initial_capital=1000, **FREE)
    res = run_backtest(bars, acts, create_strategy("buy_and_hold"), cfg)
    # Hand: buy 10 shares at the 2nd open (100). Dividend: 10 x $2 = $20 cash, uninvested.
    assert res.equity["equity"].iloc[-1] == pytest.approx(10 * 107.8 + 20)  # 1098
    # Benchmark = total return with the dividend reinvested at the ex-date close (98):
    # 1000 x (98 + 2)/100 x 107.8/98 = 1100 (it is invested from the first session).
    assert res.benchmark.iloc[-1] == pytest.approx(
        1000 * (100 / 100) * ((98 + 2) / 100) * (107.8 / 98)
    )
    # The gap is exactly the reinvestment effect: $20 that missed the +10% move.
    assert res.benchmark.iloc[-1] - res.equity["equity"].iloc[-1] == pytest.approx(20 * 0.1)


def test_benchmark_series_is_the_compounded_total_return_of_its_own_bars():
    bars = make_bars({"A": [10.0] * 6, "IDX": [100.0, 102.0, 99.0, 101.0, 49.5, 52.0]})
    d = _sessions(bars)
    acts = normalize_actions(
        pd.DataFrame(
            [
                {"symbol": "IDX", "ex_date": d[2], "kind": "dividend", "value": 1.0},
                {"symbol": "IDX", "ex_date": d[4], "kind": "split", "value": 2.0},
            ]
        )
    )
    cfg = BacktestConfig(universe=["A"], benchmark="IDX", initial_capital=1000, **FREE)
    res = run_backtest(bars, acts, create_strategy("buy_and_hold"), cfg)
    gross = [1.0, 102 / 100, (99 + 1) / 102, 101 / 99, 2 * 49.5 / 101, 52 / 49.5]
    np.testing.assert_allclose(res.benchmark.to_numpy(), 1000 * np.cumprod(gross), rtol=1e-12)


# ------------------------------------------------------------ survivorship bias
def test_dropping_names_that_later_delist_inflates_results():
    # B falls 50% and stops trading; a survivors-only universe silently drops it.
    a = [10.0] * 12
    b = [10.0, 10.0, 8.0, 6.0, 5.0, None, None, None, None, None, None, None]
    bars = make_bars({"A": a, "B": b})
    cfg = BacktestConfig(
        rebalance="quarterly", initial_capital=1000, delist_after_missing=3, **FREE
    )
    full = run_backtest(bars, NONE, create_strategy("equal_weight"), cfg)
    survivors = run_backtest(
        bars[bars["symbol"] == "A"], NONE, create_strategy("equal_weight"), cfg
    )
    # Hand: $500 in each at the 2nd open (10). B is liquidated at its last close (5).
    assert full.equity["equity"].iloc[-1] == pytest.approx(500 + 50 * 5.0)
    assert survivors.equity["equity"].iloc[-1] == pytest.approx(1000)
    liq = full.trades[full.trades["note"] == "delisting"].iloc[0]
    # Without a delisting return the exit is the last close: optimistic, as documented.
    assert liq["symbol"] == "B" and liq["price"] == 5.0
    # With the instrument master's delisting return (#9 part B) the exit is realistic:
    # B delisted on its last bar with a -60% delisting return.
    from quantlab.data.identity import normalize_master

    d = pd.DatetimeIndex(sorted(bars["date"].unique()))
    master = normalize_master(
        pd.DataFrame(
            [{"instrument_id": "B", "name": "B", "delisting_date": d[4], "delisting_return": -0.6}]
        )
    )
    real = run_backtest(bars, NONE, create_strategy("equal_weight"), cfg, instruments=master)
    assert real.equity["equity"].iloc[-1] == pytest.approx(500 + 50 * 5.0 * 0.4)
    # The survivorship gap widens once delisting losses are counted: 1000 vs 600.
    assert survivors.equity["equity"].iloc[-1] - real.equity["equity"].iloc[-1] == pytest.approx(
        400
    )


# ---------------------------------------------------------- missing observations
def test_a_gap_in_one_symbol_does_not_move_another_symbols_fills_or_marks():
    # Both are held (bought once, never rebalanced), so A's gap is exercised on marks.
    a = [10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5]
    b = [20.0, 21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0]
    gappy_a = [10.0, 10.5, None, None, 12.0, 12.5, 13.0, 13.5]
    cfg = BacktestConfig(rebalance="daily", initial_capital=10_000, **FREE)

    class BuyOnce(Strategy):
        key, name, description, Params = "buy_once", "", "", StrategyParams

        def target_weights(self, view: MarketView) -> pd.Series | None:
            return pd.Series({"A": 0.4, "B": 0.5}) if len(view.close) == 1 else None

    clean = run_backtest(make_bars({"A": a, "B": b}), NONE, BuyOnce(), cfg)
    gappy = run_backtest(make_bars({"A": gappy_a, "B": b}), NONE, BuyOnce(), cfg)
    pd.testing.assert_frame_equal(clean.trades, gappy.trades)  # bought at d[1] in both
    pb = [r.positions[r.positions["symbol"] == "B"].reset_index(drop=True) for r in (clean, gappy)]
    cols = ["date", "quantity", "price"]
    pd.testing.assert_frame_equal(pb[0][cols], pb[1][cols])
    # A is marked at its last close (10.5) through the gap; equity differs by exactly that.
    qa = 0.4 * 10_000 / 10.0  # shares are sized on the decision close (10.0), filled next open
    d = pd.DatetimeIndex(sorted(make_bars({"A": a})["date"].unique()))
    pa = gappy.positions[gappy.positions["symbol"] == "A"].set_index("date")["price"]
    assert pa.loc[d[2]] == pa.loc[d[3]] == 10.5
    gap = gappy.equity["equity"] - clean.equity["equity"]
    np.testing.assert_allclose(gap.loc[[d[2], d[3]]].to_numpy(), [qa * -0.5, qa * -1.0])
    np.testing.assert_allclose(gap.drop(index=[d[2], d[3]]).to_numpy(), 0.0, atol=1e-9)


# ---------------------------------------------------------- corrected history
def test_a_corrected_past_bar_changes_the_version_and_spares_earlier_decisions(market):
    fixed = market.bars.copy()
    sym = sorted(fixed["symbol"].unique())[2]
    rows = fixed.index[fixed["symbol"] == sym]
    when = fixed.loc[rows[len(rows) // 2], "date"]
    fixed.loc[rows[len(rows) // 2], ["close", "high"]] *= 1.07  # a vendor correction of one bar
    assert content_hash(fixed, market.actions) != content_hash(market.bars, market.actions)
    cfg = BacktestConfig(rebalance="monthly")
    before = run_backtest(market.bars, market.actions, create_strategy("momentum"), cfg)
    after = run_backtest(fixed, market.actions, create_strategy("momentum"), cfg)
    early = [r.weights_target[r.weights_target["date"] < when] for r in (before, after)]
    pd.testing.assert_frame_equal(early[0].reset_index(drop=True), early[1].reset_index(drop=True))


# ------------------------------------------------------------- reproducibility
def test_results_do_not_depend_on_input_row_order(market):
    cfg = BacktestConfig(rebalance="monthly", benchmark="SYNIDX")
    a = run_backtest(market.bars, market.actions, create_strategy("low_volatility"), cfg)
    shuffled_bars = market.bars.sample(frac=1.0, random_state=1).reset_index(drop=True)
    shuffled_acts = market.actions.sample(frac=1.0, random_state=2).reset_index(drop=True)
    b = run_backtest(shuffled_bars, shuffled_acts, create_strategy("low_volatility"), cfg)
    assert a.fingerprint() == b.fingerprint()
    pd.testing.assert_series_equal(a.benchmark, b.benchmark)
    assert content_hash(market.bars, market.actions) == content_hash(shuffled_bars, shuffled_acts)
    pa = compute_performance(a.equity["equity"], a.benchmark).to_dict()
    pb = compute_performance(b.equity["equity"], b.benchmark).to_dict()
    assert pa == pb
