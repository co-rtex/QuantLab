"""The integrity report compares the validation's engine with the experiment's (#155)."""

from __future__ import annotations

import pytest

from quantlab.research.integrity import build_integrity_report

NOT_COMPARABLE = (
    "These results are not directly comparable under identical engine rules. Reproduce the "
    "experiment on engine 1.4.0 and validate the reproduction for a like-for-like comparison."
)


def _check(validation: dict | None, experiment_engine: str = "1.4.0") -> dict | None:
    ev = {
        "experiment": {"config": {}, "engine_version": experiment_engine},
        "dataset": {},
        "validation": validation,
    }
    return {c["key"]: c for c in build_integrity_report(ev)["checks"]}.get("validation_engine")


@pytest.mark.parametrize(
    ("validation", "experiment_engine", "status", "comparable"),
    [
        ({"engine_version": "1.4.0"}, "1.4.0", "verified", True),
        ({"engine_version": "1.5.0"}, "1.4.0", "warning", False),
        ({"walk_forward": {}}, "1.4.0", "unassessed", None),  # a run recorded before #155
        ({"engine_version": "1.4.0"}, "", "unassessed", None),  # experiment unstamped
    ],
)
def test_the_validation_engine_is_compared_with_the_experiments(
    validation, experiment_engine, status, comparable
):
    check = _check(validation, experiment_engine)
    assert check is not None and check["status"] == status
    assert check["evidence"]["comparable_engine"] is comparable


def test_a_mismatch_is_flagged_as_not_directly_comparable():
    check = _check({"engine_version": "1.4.0"}, experiment_engine="1.3.0")
    assert check["finding"] == (
        "The validation ran on engine 1.4.0; the experiment was computed on engine 1.3.0. "
        + NOT_COMPARABLE
    )
    assert check["evidence"]["comparable_engine"] is False


@pytest.mark.parametrize(
    ("validation", "experiment_engine", "names"),
    [
        ({"walk_forward": {}}, "1.4.0", "The validation run does not record"),
        ({"engine_version": "1.4.0"}, "", "The experiment does not record"),
        ({"walk_forward": {}}, "", "Neither the validation run nor the experiment"),
    ],
)
def test_an_unassessed_check_names_the_side_that_is_unrecorded(
    validation, experiment_engine, names
):
    finding = _check(validation, experiment_engine)["finding"]
    assert finding.startswith(names)
    if experiment_engine:  # only the run is unrecorded: it is an old run
        assert "before #155" in finding
    else:
        assert "before #155" not in finding


def test_there_is_no_check_without_a_validation_run():
    assert _check(None) is None
