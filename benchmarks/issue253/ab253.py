"""#253: does the min_variance strategy ever hit an SLSQP failure in backtests? On failure
the strategy keeps its holdings (returns None), so accepting KKT-verified points would
change those runs. Counts on the A/B grid's min_variance runs (tests/oracle/ab.py: small
params, weekly, both executions, three accounts) and default-param weekly and daily runs.
Synthetic only."""

import sys
from collections import Counter

from quantlab.backtest.engine import BacktestConfig, run_backtest
from quantlab.data.synthetic import BENCHMARK_SYMBOL, SyntheticSpec, generate_market
from quantlab.optimize import allocators
from quantlab.strategies import library

calls, fails = Counter(), Counter()
messages = Counter()
real = allocators.min_variance


def counting(cov, bounds=None):
    calls[current] += 1
    try:
        return real(cov, bounds)
    except allocators.OptimizationError as exc:
        fails[current] += 1
        messages[str(exc)] += 1
        raise


library.min_variance = counting
for mseed in (3, 11, 29):
    m = generate_market(SyntheticSpec(n_assets=30, start="2019-01-02", end="2021-12-31", seed=mseed))
    for execution in ("next_open", "next_close"):
        for fractional, capital in ((False, 1_000_000.0), (True, 20_000.0), (True, 3e9)):
            current = "ab_grid"
            cfg = BacktestConfig(execution=execution, rebalance="weekly", fractional_shares=fractional,
                                 initial_capital=capital, benchmark=BENCHMARK_SYMBOL)
            run_backtest(m.bars, m.actions, library.create_strategy("min_variance", {"lookback": 30}), cfg,
                         instruments=m.master)
    for rebalance in ("weekly", "daily"):
        current = f"defaults_{rebalance}"
        cfg = BacktestConfig(rebalance=rebalance, benchmark=BENCHMARK_SYMBOL)
        run_backtest(m.bars, m.actions, library.create_strategy("min_variance", {}), cfg, instruments=m.master)
print("platform:", sys.platform)
for k in calls:
    print(f"  {k}: {calls[k]} solves, {fails[k]} failed")
print("  failure messages:", dict(messages))
