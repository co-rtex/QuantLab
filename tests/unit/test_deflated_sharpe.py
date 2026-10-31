"""Deflated Sharpe: trials and the selected strategy share one window and rf (issue #14)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from quantlab.analytics.performance import simple_returns
from quantlab.backtest.engine import BacktestConfig, run_backtest
from quantlab.data.synthetic import SyntheticSpec, generate_market
from quantlab.research.stats import per_period_sharpe, probabilistic_sharpe
from quantlab.research.suite import run_validation_suite
from quantlab.research.validation import invested_equity, param_grid, resolve_params
from quantlab.strategies.library import create_strategy

RF = 0.03


@pytest.fixture(scope="module")
def small_market():
    return generate_market(
        SyntheticSpec(n_assets=12, start="2019-01-02", end="2022-12-30", seed=11)
    )


def test_psr_measures_excess_over_the_risk_free_rate():
    rng = np.random.default_rng(0)
    r = pd.Series(rng.normal(0.0004, 0.01, 750))
    rf_p = (1 + RF) ** (1 / 252) - 1
    with_rf = probabilistic_sharpe(r, risk_free_rate=RF)
    assert with_rf == pytest.approx(probabilistic_sharpe(r - rf_p), rel=1e-12)
    assert with_rf < probabilistic_sharpe(r)  # main ignored rf: overstated PSR


def test_per_period_sharpe_matches_annualized_sharpe_convention():
    rng = np.random.default_rng(1)
    r = pd.Series(rng.normal(0.0004, 0.01, 500))
    rf_p = (1 + RF) ** (1 / 252) - 1
    ex = r - rf_p
    assert per_period_sharpe(r, risk_free_rate=RF) == pytest.approx(
        ex.mean() / ex.std(ddof=1), rel=1e-12
    )
    assert per_period_sharpe(pd.Series(np.full(50, 0.001))) is None


@pytest.mark.parametrize(
    "base", [{}, {"lookback": 252, "skip": 21, "top_n": 10}, {"lookback": 200}]
)
def test_selected_parameters_are_always_exactly_one_trial(base):
    # main compared raw dicts, so default parameters ({}) were never marked as the base
    # and were missing from the trials whenever the defaults were off the grid.
    combos = param_grid("momentum", base, None)
    target = resolve_params("momentum", base)
    assert sum(resolve_params("momentum", c) == target for c in combos) == 1


def test_trials_and_selected_strategy_use_the_same_window_and_rf(small_market):
    m = small_market
    cfg = BacktestConfig(risk_free_rate=RF)
    out = run_validation_suite(m.bars, m.actions, "momentum", {}, cfg)
    rows = out["sensitivity"]["rows"]
    base = next(r for r in rows if r["is_base"])
    dsr = out["deflated"]

    # Each trial's per-period Sharpe is the same statistic as its reported annualized
    # Sharpe (invested window, excess of rf); main computed it without rf.
    for row in rows:
        if row["sharpe"] is not None:
            assert row["sharpe_per_period"] * math.sqrt(252) == pytest.approx(
                row["sharpe"], rel=1e-9
            )
    # The selected strategy is deflated on exactly the returns that produced its trial.
    assert dsr["selected_sharpe_per_period"] == pytest.approx(base["sharpe_per_period"], 1e-12)
    assert dsr["observations"] == len(
        simple_returns(
            invested_equity(
                run_backtest(
                    m.bars, m.actions, create_strategy("momentum", {}), cfg, record_positions=False
                ).equity
            )
        )
    )

    # The old inputs disagreed: the full window (a year of warm-up cash) without rf.
    full = run_backtest(m.bars, m.actions, create_strategy("momentum", {}), cfg)
    old = per_period_sharpe(simple_returns(full.equity["equity"]))
    assert old is not None
    assert abs(old - base["sharpe_per_period"]) > 0.002


def test_trials_without_a_sharpe_still_count_toward_n():
    from quantlab.research.stats import deflated_sharpe, expected_max_sharpe

    sharpes = [0.01, -0.02, 0.03, 0.0]
    assert expected_max_sharpe(sharpes, n_trials=10) > expected_max_sharpe(sharpes)
    rng = np.random.default_rng(4)
    r = pd.Series(rng.normal(0.0005, 0.01, 500))
    few = deflated_sharpe(r, sharpes)
    all_tried = deflated_sharpe(r, sharpes, n_trials=10)
    assert all_tried["trials"] == 10 and all_tried["trials_with_sharpe"] == 4
    assert all_tried["deflated_sharpe"] <= few["deflated_sharpe"]  # more conservative


def test_suite_counts_every_grid_point_as_a_trial(small_market):
    m = small_market
    out = run_validation_suite(m.bars, m.actions, "momentum", {}, BacktestConfig())
    assert out["deflated"]["trials"] == len(out["sensitivity"]["rows"])
