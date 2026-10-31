"""The integrity report compares the validation's engine with the experiment's (#155)."""

from __future__ import annotations

import pytest

from quantlab.research.integrity import build_integrity_report


def _check(validation: dict | None, experiment_engine: str = "1.4.0") -> dict | None:
    ev = {
        "experiment": {"config": {}, "engine_version": experiment_engine},
        "dataset": {},
        "validation": validation,
    }
    return {c["key"]: c for c in build_integrity_report(ev)["checks"]}.get("validation_engine")


@pytest.mark.parametrize(
    ("validation", "status"),
    [
        ({"engine_version": "1.4.0"}, "verified"),
        ({"engine_version": "1.4.0", "walk_forward": {}}, "verified"),
        ({"engine_version": "1.5.0"}, "warning"),
        ({"walk_forward": {}}, "unassessed"),  # a run recorded before #155
    ],
)
def test_the_validation_engine_is_compared_with_the_experiments(validation, status):
    check = _check(validation)
    assert check is not None and check["status"] == status


def test_a_mismatch_names_both_engines_and_the_remedy():
    finding = _check({"engine_version": "1.4.0"}, experiment_engine="1.3.0")["finding"]
    assert "ran on engine 1.4.0" in finding and "computed on engine 1.3.0" in finding
    assert "Reproduce the experiment" in finding


def test_there_is_no_check_without_a_validation_run():
    assert _check(None) is None
