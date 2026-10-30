"""Validation suite and Research Integrity Report through the API."""

from __future__ import annotations

from tests.integration.conftest import run_pending_jobs
from tests.integration.test_api_workflow import submit


def _completed_experiment(client, seeded):
    r = submit(client, seeded, params={"lookback": 63, "skip": 5, "top_n": 4})
    assert run_pending_jobs() == ["completed"]
    return r.json()["experiment"]["id"]


def test_integrity_before_and_after_validation(client, seeded):
    h = seeded["headers"]
    exp_id = _completed_experiment(client, seeded)
    rep = client.get(f"/api/v1/experiments/{exp_id}/integrity", headers=h)
    assert rep.status_code == 200, rep.text
    by = {c["key"]: c for c in rep.json()["checks"]}
    assert by["dataset_provenance"]["status"] == "warning"  # synthetic data is disclosed
    assert by["lookahead"]["status"] == "unassessed"
    assert by["out_of_sample"]["status"] == "unassessed"
    assert by["execution_timing"]["status"] == "verified"

    start = client.post(f"/api/v1/experiments/{exp_id}/validation", headers=h)
    assert start.status_code == 202, start.text
    assert start.json()["status"] == "queued"
    again = client.post(f"/api/v1/experiments/{exp_id}/validation", headers=h)
    assert again.json()["id"] == start.json()["id"]  # no duplicate run while queued
    assert run_pending_jobs() == ["completed"]

    val = client.get(f"/api/v1/experiments/{exp_id}/validation", headers=h).json()
    assert val["status"] == "completed"
    res = val["result"]
    assert res["leakage"]["status"] == "verified"
    assert {"walk_forward", "sensitivity", "cost", "bootstrap", "deflated", "regimes"} <= set(res)
    assert "oos_returns" not in res["walk_forward"]

    rep = client.get(f"/api/v1/experiments/{exp_id}/integrity", headers=h).json()
    by = {c["key"]: c for c in rep["checks"]}
    assert by["lookahead"]["status"] == "verified"
    assert by["out_of_sample"]["status"] in ("verified", "warning")
    assert by["statistical_uncertainty"]["status"] in ("verified", "warning")
    assert by["multiple_testing"]["status"] in ("verified", "warning")

    client.post(f"/api/v1/experiments/{exp_id}/reproduce", headers=h)
    assert run_pending_jobs() == ["completed"]
    rep = client.get(f"/api/v1/experiments/{exp_id}/integrity", headers=h).json()
    assert {c["key"]: c for c in rep["checks"]}["reproducibility"]["status"] == "verified"


def test_validation_requires_completed_backtest_and_permissions(client, seeded):
    from tests.integration.conftest import register

    h = seeded["headers"]
    r = submit(client, seeded).json()
    exp_id = r["experiment"]["id"]
    early = client.post(f"/api/v1/experiments/{exp_id}/validation", headers=h)
    assert early.status_code == 409
    assert client.get(f"/api/v1/experiments/{exp_id}/integrity", headers=h).status_code == 409
    other = register(client, "outsider@example.com")
    assert client.post(f"/api/v1/experiments/{exp_id}/validation", headers=other).status_code == 404
