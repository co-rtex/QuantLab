import itertools

import numpy as np
import pandas as pd
import pytest

from quantlab.backtest.engine import BacktestConfig
from quantlab.data import adjust
from quantlab.data.synthetic import SyntheticSpec, generate_market
from quantlab.research.integrity import build_integrity_report
from quantlab.research.stats import (
    bootstrap_ci,
    deflated_sharpe,
    expected_max_sharpe,
    probabilistic_sharpe,
    regime_analysis,
)
from quantlab.research.validation import (
    cost_sensitivity,
    leakage_check,
    make_folds,
    param_grid,
    sensitivity,
    walk_forward,
)
from quantlab.strategies.base import MarketView, Strategy, StrategyParams, top_n
from quantlab.strategies.library import REGISTRY

CFG = BacktestConfig(start="2020-01-02", benchmark="SYNIDX", commission_bps=1, slippage_bps=5)


def test_param_grid_skips_invalid_combinations():
    combos = param_grid(
        "sma_crossover", {"fast": 50, "slow": 200}, {"fast": [20, 100, 300], "slow": [100, 200]}
    )
    assert all(c["fast"] < c["slow"] for c in combos)
    assert {"fast": 50, "slow": 200} in combos  # base always included
    assert param_grid("equal_weight", {}, None) == [{}]


def test_folds_are_chronological_with_embargo():
    sessions = pd.bdate_range("2018-01-01", periods=1500)
    folds = make_folds(sessions, train_sessions=500, test_sessions=250, embargo=5)
    assert len(folds) >= 3
    for f in folds:
        gap = sessions.get_loc(f.test_start) - sessions.get_loc(f.train_end)
        assert gap == 6  # 5 embargo sessions between train end and test start
        assert f.train_start < f.train_end < f.test_start <= f.test_end
    for a, b in itertools.pairwise(folds):
        assert b.test_start > a.test_end  # test windows never overlap


def test_walk_forward_selects_in_sample_and_tests_out_of_sample(market):
    wf = walk_forward(
        market.bars,
        market.actions,
        "momentum",
        {"lookback": 126, "skip": 21, "top_n": 5},
        CFG,
        grid={"lookback": [63, 126], "top_n": [3, 5]},
        train_sessions=252,
        test_sessions=126,
    )
    assert len(wf["folds"]) >= 2
    for f in wf["folds"]:
        assert f["test"][0] > f["train"][1]
        assert f["selected_params"]["lookback"] in (63, 126)
        best = max(
            (g for g in f["in_sample_grid"] if g["sharpe"] is not None), key=lambda g: g["sharpe"]
        )
        assert f["in_sample_sharpe"] == pytest.approx(best["sharpe"])
    oos_dates = pd.to_datetime(wf["oos_equity"]["dates"])
    assert oos_dates.min() >= pd.Timestamp(wf["folds"][0]["test"][0])
    assert oos_dates.is_monotonic_increasing


def test_cost_sweep_monotone_with_fractional_shares(market):
    cfg = CFG.model_copy(update={"fractional_shares": True})
    out = cost_sensitivity(
        market.bars, market.actions, "equal_weight", {}, cfg, multipliers=(0, 1, 4, 16)
    )
    cagr = [r["cagr"] for r in out["rows"]]
    assert all(a >= b for a, b in itertools.pairwise(cagr))
    assert out["rows"][0]["round_trip_bps"] == 0


def test_sensitivity_summary(market):
    out = sensitivity(
        market.bars,
        market.actions,
        "low_volatility",
        {"lookback": 63, "top_n": 10, "weighting": "inverse_vol"},
        CFG,
        grid={"lookback": [63, 126], "top_n": [5, 10]},
    )
    assert out["summary"]["grid_size"] == 4
    assert len(out["trial_sharpes_per_period"]) == 4
    assert out["summary"]["base_rank"] in (1, 2, 3, 4)


def test_leakage_check_passes_for_library_strategies(market):
    for key, params in [
        ("momentum", {"lookback": 126, "skip": 21, "top_n": 5}),
        ("sma_crossover", {"fast": 20, "slow": 100}),
    ]:
        res = leakage_check(market.bars, market.actions, key, params, CFG, samples=4)
        assert res["status"] == "verified", res


class LowestPrice(Strategy):
    """Depends on adjusted price *levels*, so it is sensitive to adjustment as-of dates."""

    key = "test_lowest_price"
    name = "lowest price"
    description = ""
    Params = StrategyParams

    def target_weights(self, view: MarketView):
        picks = top_n(view.close.iloc[-1][view.universe], 3, ascending=True)
        return pd.Series(1 / 3, index=picks)


def test_leakage_check_detects_planted_lookahead(monkeypatch):
    m = generate_market(
        SyntheticSpec(
            n_assets=15, start="2019-01-02", end="2021-12-31", seed=4, split_probability=0.01
        )
    )
    REGISTRY[LowestPrice.key] = LowestPrice
    try:
        ok = leakage_check(m.bars, m.actions, LowestPrice.key, {}, CFG, samples=10)
        assert ok["status"] == "verified"
        # Plant a bug: adjust with *all* corporate actions regardless of the as-of date.
        real = adjust.PointInTimeAdjuster.multiplier
        monkeypatch.setattr(
            adjust.PointInTimeAdjuster, "multiplier", lambda self, as_of_pos=None: real(self, None)
        )
        leaky = leakage_check(m.bars, m.actions, LowestPrice.key, {}, CFG, samples=10)
        assert leaky["status"] == "failed"
        assert leaky["mismatches"]
    finally:
        REGISTRY.pop(LowestPrice.key, None)


def test_psr_dsr_properties():
    rng = np.random.default_rng(0)
    strong = pd.Series(rng.normal(0.002, 0.01, 1000))
    noise = pd.Series(rng.normal(0.0, 0.01, 1000))
    assert probabilistic_sharpe(strong) > 0.99
    assert 0.0 < probabilistic_sharpe(noise) < 0.99
    trials = list(rng.normal(0, 0.03, 20))
    d = deflated_sharpe(strong, trials)
    assert d["deflated_sharpe"] <= d["psr_vs_zero"]
    assert expected_max_sharpe(trials[:5]) < expected_max_sharpe(trials * 10)
    assert expected_max_sharpe([0.1]) == 0.0


def test_bootstrap_deterministic_and_brackets_estimate():
    rng = np.random.default_rng(1)
    r = pd.Series(rng.normal(0.0005, 0.01, 750), index=pd.bdate_range("2020-01-01", periods=750))
    b = pd.Series(rng.normal(0.0003, 0.01, 750), index=r.index)
    a1 = bootstrap_ci(r, b, n_samples=300, seed=5)
    a2 = bootstrap_ci(r, b, n_samples=300, seed=5)
    assert a1 == a2
    assert a1["sharpe"]["lower"] < a1["sharpe"]["estimate"] < a1["sharpe"]["upper"]
    assert a1["excess_return"]["lower"] < a1["excess_return"]["upper"]
    assert "error" in bootstrap_ci(r.iloc[:10])
    regimes = regime_analysis(r, b)
    assert {x["dimension"] for x in regimes} == {"vol", "trend"}
    assert sum(x["sessions"] for x in regimes if x["dimension"] == "vol") == sum(
        x["sessions"] for x in regimes if x["dimension"] == "trend"
    )


def _evidence(**over):
    ev = {
        "experiment": {
            "config": {
                "commission_bps": 1,
                "slippage_bps": 5,
                "benchmark": "IDX",
                "universe": ["A", "B"],
            },
            "engine_version": "1.0.0",
            "input_hash": "h",
            "fingerprint": "f",
        },
        "dataset": {
            "version_hash": "v",
            "provenance": {"provider": "csv"},
            "is_synthetic": False,
            "quality_status": "pass",
            "delisted_symbols": ["C"],
        },
        "analysis": {"meta": {"sessions": 1300, "universe": ["A", "B"]}},
    }
    for k, v in over.items():
        ev[k] = v
    return ev


def test_integrity_report_statuses():
    rep = build_integrity_report(_evidence())
    by = {c["key"]: c for c in rep["checks"]}
    assert by["dataset_provenance"]["status"] == "verified"
    assert by["universe"]["status"] == "warning"  # survivors only: C was delisted
    assert by["lookahead"]["status"] == "unassessed"
    assert by["out_of_sample"]["status"] == "unassessed"
    assert by["reproducibility"]["status"] == "unassessed"
    assert sum(rep["summary"].values()) == len(rep["checks"])
    assert "nothing in it is investment advice" in rep["disclaimer"]

    zero_cost = _evidence(experiment={"config": {"commission_bps": 0, "slippage_bps": 0}})
    assert {c["key"]: c for c in build_integrity_report(zero_cost)["checks"]}["transaction_costs"][
        "status"
    ] == "warning"

    bad = _evidence(
        validation={"leakage": {"status": "failed", "mismatches": ["2020-01-02"]}},
        reproductions=[{"status": "completed", "fingerprint_match": False}],
    )
    by = {c["key"]: c for c in build_integrity_report(bad)["checks"]}
    assert by["lookahead"]["status"] == "failed"
    assert by["reproducibility"]["status"] == "failed"

    good = _evidence(
        reproductions=[{"status": "completed", "fingerprint_match": True}],
        validation={"bootstrap": {"sharpe": {"lower": 0.2, "upper": 1.1}}},
    )
    by = {c["key"]: c for c in build_integrity_report(good)["checks"]}
    assert by["reproducibility"]["status"] == "verified"
    assert by["statistical_uncertainty"]["status"] == "verified"


def test_walk_forward_windows_adapt_to_sample():
    from quantlab.research.suite import walk_forward_windows

    assert walk_forward_windows(1500) == (504, 252)
    assert walk_forward_windows(500) == (250, 125)
    assert walk_forward_windows(100)[0] == 126  # too short: walk-forward reports an error
