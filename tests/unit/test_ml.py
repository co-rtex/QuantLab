import numpy as np
import pandas as pd
import pytest

from quantlab.ml.pipeline import (
    FEATURES,
    MLConfig,
    _metrics,
    build_panel,
    compute_features,
    purged_splits,
    run_ml_experiment,
)


@pytest.fixture(scope="module")
def result(market):
    return run_ml_experiment(
        market.bars, market.actions, MLConfig(folds=3, min_train_dates=12, sample_every=10)
    )


def test_features_use_no_future_data(market):
    full, _ = compute_features(market.bars, market.actions)
    for d in pd.DatetimeIndex(sorted(market.bars["date"].unique()))[[300, 500, 700]]:
        tb = market.bars[market.bars["date"] <= d]
        ta = market.actions[market.actions["ex_date"] <= d]
        trunc, _ = compute_features(tb, ta)
        for name in FEATURES:
            a = full[name].loc[d].dropna().sort_index()
            b = trunc[name].loc[d].dropna().sort_index()
            pd.testing.assert_series_equal(a, b, check_names=False, rtol=1e-9)


def test_labels_and_availability(market):
    cfg = MLConfig(sample_every=21)
    panel = build_panel(market.bars, market.actions, cfg)
    assert (panel["label_available_at"] > panel["date"]).all()
    rate = panel.groupby("date")["label"].mean()
    assert rate.between(0.4, 0.6).all()  # median split by construction
    _, adj = compute_features(market.bars, market.actions)
    row = panel.iloc[len(panel) // 2]
    k = adj.index.get_loc(row["date"])
    expected = adj[row["symbol"]].iloc[k + cfg.horizon] / adj[row["symbol"]].iloc[k] - 1
    assert row["fwd_return"] == pytest.approx(expected)


def test_purged_splits_never_train_on_overlapping_labels(market):
    cfg = MLConfig(folds=3, min_train_dates=12, sample_every=10)
    panel = build_panel(market.bars, market.actions, cfg)
    for sp in purged_splits(panel, cfg):
        train = panel[panel["date"].isin(sp.train_dates)]
        assert train["label_available_at"].max() < sp.test_dates[0]
        assert sp.train_dates.max() < sp.test_dates.min()


def test_experiment_is_deterministic_and_reports_baselines(market, result):
    again = run_ml_experiment(
        market.bars, market.actions, MLConfig(folds=3, min_train_dates=12, sample_every=10)
    )
    assert again["panel"]["hash"] == result["panel"]["hash"]
    assert [s["auc"] for s in again["summary"]] == [s["auc"] for s in result["summary"]]
    models = {s["model"] for s in result["summary"]}
    assert {"majority", "momentum_rule", "logistic"} <= models
    assert set(result["feature_importance"]["random_forest"]) == set(FEATURES)
    assert len(result["predictions"]) == len(models) * sum(
        r["rows"] for r in result["folds"] if r["model"] == "majority"
    )


def test_metrics_reference_values():
    dates = np.repeat(pd.bdate_range("2024-01-01", periods=3), 20)
    fwd = np.tile(np.linspace(-0.1, 0.1, 20), 3)
    test = pd.DataFrame(
        {"date": dates, "fwd_return": fwd, "label": (fwd > np.median(fwd)).astype(int)}
    )
    perfect = _metrics(test, fwd + 0.5)
    assert perfect["auc"] == pytest.approx(1.0)
    assert perfect["ic_mean"] == pytest.approx(1.0)
    assert perfect["top_minus_bottom_quintile"] > 0
    inverted = _metrics(test, 0.5 - fwd)
    assert inverted["auc"] == pytest.approx(0.0)
    flat = _metrics(test, np.full(len(test), 0.5))
    assert flat["auc"] is None  # undefined for a constant score, not 0.5
    assert flat["ic_mean"] is None
