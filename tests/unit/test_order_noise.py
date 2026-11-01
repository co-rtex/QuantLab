"""Float-noise rebalance orders are not trades (#215). Synthetic data only.

A target equal to the holding up to float rounding leaves a share change of a few units in
the last place of the holding. Engine 1.6.0 creates no order for a change under 1e-9
shares or under a trillionth of the position; before, each such order filled and paid a
full commission_min.
"""

from __future__ import annotations

import math

import pandas as pd

from quantlab.backtest.engine import BacktestConfig, order_is_noise, run_backtest
from quantlab.data.schema import normalize_actions, normalize_bars
from quantlab.strategies.library import create_strategy


def test_a_change_of_exactly_the_threshold_is_a_trade_and_one_below_is_not():
    held = want = 1e6
    threshold = 1e-12 * 1e6
    assert not order_is_noise(threshold, want, held)  # exactly the threshold: a trade
    assert order_is_noise(math.nextafter(threshold, 0.0), want, held)
    # The absolute floor for small positions: 1e-9 shares.
    assert not order_is_noise(1e-9, 1.0, 1.0)
    assert order_is_noise(math.nextafter(1e-9, 0.0), 1.0, 1.0)
    # Selling out is never noise, however large the position.
    assert not order_is_noise(-8e6, 0.0, 8e6)


def test_one_ulp_of_a_large_position_is_noise():
    held = 2.0**23  # 8,388,608 shares: one unit in the last place is 2^-29 shares
    want = math.nextafter(held, math.inf)
    assert want - held == 2.0**-29
    assert order_is_noise(want - held, want, held)


def test_a_real_small_order_is_never_skipped():
    # Capital 100, whole shares, a price of 50: half the account is one share.
    days = pd.bdate_range("2024-01-02", periods=3)
    bars = normalize_bars(
        pd.DataFrame(
            {
                "symbol": "AAA",
                "date": days,
                "open": 50.0,
                "high": 50.0,
                "low": 50.0,
                "close": 50.0,
                "volume": 1e4,
            }
        )
    )
    cfg = BacktestConfig(
        initial_capital=100.0, rebalance="daily", commission_bps=0, slippage_bps=0
    )
    res = run_backtest(bars, normalize_actions(None), create_strategy("equal_weight"), cfg)
    assert res.trades["quantity"].tolist()[:1] == [2]  # 100 / 50: the whole account
    weights = BacktestConfig(
        initial_capital=100.0,
        rebalance="daily",
        commission_bps=0,
        slippage_bps=0,
        max_weight=0.5,
    )
    half = run_backtest(bars, normalize_actions(None), create_strategy("equal_weight"), weights)
    assert half.trades["quantity"].tolist()[:1] == [1]  # a single share is a trade
