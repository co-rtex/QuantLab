"""Run the standard validation suite for one experiment."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd

from quantlab.analytics.performance import simple_returns
from quantlab.analytics.summary import jsonable
from quantlab.backtest.engine import BacktestConfig, run_backtest
from quantlab.research.stats import bootstrap_ci, deflated_sharpe, regime_analysis
from quantlab.research.validation import (
    cost_sensitivity,
    invested_equity,
    leakage_check,
    sensitivity,
    walk_forward,
)
from quantlab.strategies.library import create_strategy

STEPS = ("baseline", "leakage", "sensitivity", "cost", "walk_forward")
MIN_TRAIN_SESSIONS = 126


def walk_forward_windows(sessions: int) -> tuple[int, int]:
    """Two years train / one year test when the sample allows at least two folds;
    otherwise half the sample for training and a quarter for each test window."""
    if sessions >= 504 + 2 * 252:
        return 504, 252
    return max(MIN_TRAIN_SESSIONS, sessions // 2), max(21, sessions // 4)


def run_validation_suite(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    strategy_key: str,
    params: dict[str, Any],
    config: BacktestConfig,
    *,
    on_progress: Callable[[float, str], None] | None = None,
    seed: int = 0,
) -> dict[str, Any]:
    def stage(i: int) -> Callable[[float, str], None]:
        def report(f: float, msg: str) -> None:
            if on_progress:
                on_progress((i + f) / len(STEPS), msg)

        return report

    stage(0)(0.0, "baseline backtest")
    base = run_backtest(
        bars, actions, create_strategy(strategy_key, params), config, record_positions=False
    )
    r = simple_returns(base.equity["equity"])
    b = simple_returns(base.benchmark) if base.benchmark is not None else None
    out: dict[str, Any] = {
        "bootstrap": bootstrap_ci(r, b, seed=seed, risk_free_rate=config.risk_free_rate),
        "regimes": regime_analysis(r, b) if b is not None else [],
    }
    stage(1)(0.0, "look-ahead check")
    out["leakage"] = leakage_check(bars, actions, strategy_key, params, config, seed=seed)
    out["sensitivity"] = sensitivity(
        bars, actions, strategy_key, params, config, on_progress=stage(2)
    )
    trials = out["sensitivity"].pop("trial_sharpes_per_period")
    # Deflate the selected run on the returns that produced its trial Sharpe: the invested
    # window and the same rf as every other trial.
    out["deflated"] = deflated_sharpe(
        simple_returns(invested_equity(base.equity)),
        trials,
        risk_free_rate=config.risk_free_rate,
    )
    out["cost"] = cost_sensitivity(
        bars, actions, strategy_key, params, config, on_progress=stage(3)
    )
    train, test = walk_forward_windows(len(base.equity))
    wf = walk_forward(
        bars,
        actions,
        strategy_key,
        params,
        config,
        train_sessions=train,
        test_sessions=test,
        on_progress=stage(4),
    )
    wf.pop("oos_returns", None)
    out["walk_forward"] = wf
    return jsonable(out)
