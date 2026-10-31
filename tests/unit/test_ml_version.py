"""A run is computed only by the pipeline version it was submitted for (#143)."""

from __future__ import annotations

import pytest

from quantlab.data.schema import normalize_actions
from quantlab.ml.pipeline import ML_PIPELINE_VERSION, MLConfig, run_ml_experiment
from tests.unit.test_ml_own_bars import _bars

SMALL = {"horizon": 5, "sample_every": 1, "folds": 2, "min_train_dates": 6, "models": ("majority",)}


def test_a_run_stamped_for_another_version_is_refused():
    cfg = MLConfig(**SMALL, pipeline_version=ML_PIPELINE_VERSION - 1)
    with pytest.raises(ValueError, match="submitted for pipeline version"):
        run_ml_experiment(_bars(), normalize_actions(None), cfg)


@pytest.mark.parametrize("stamp", [None, ML_PIPELINE_VERSION])
def test_a_matching_or_unstamped_run_is_computed(stamp):
    out = run_ml_experiment(
        _bars(), normalize_actions(None), MLConfig(**SMALL, pipeline_version=stamp)
    )
    assert out["pipeline_version"] == ML_PIPELINE_VERSION
