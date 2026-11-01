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
    assert by["validation_engine"]["status"] == "verified"  # same engine as the experiment
    assert val["comparable_engine"] is True

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


def test_report_export_html_and_markdown(client, seeded):
    h = seeded["headers"]
    exp_id = _completed_experiment(client, seeded)
    client.post(f"/api/v1/experiments/{exp_id}/validation", headers=h)
    run_pending_jobs()
    r = client.get(f"/api/v1/experiments/{exp_id}/report?format=html", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    for directive in ("default-src 'none'", "sandbox", "form-action 'none'", "base-uri 'none'"):
        assert directive in csp, directive
    body = r.text
    assert "Synthetic data." in body
    assert "<svg" in body
    assert "<script" not in body
    assert "Research integrity" in body
    assert "Out-of-sample validation" in body
    md = client.get(f"/api/v1/experiments/{exp_id}/report?format=md&download=true", headers=h)
    assert md.headers["content-disposition"].endswith('.md"')
    assert md.text.startswith("# Momentum test")
    assert "| Sharpe ratio |" in md.text
    assert "> **Synthetic data.**" in md.text  # labelled in Markdown too (#230)
    exp = client.get(f"/api/v1/experiments/{exp_id}", headers=h).json()
    assert exp["fingerprint"] in md.text  # report cites the exact output fingerprint


def test_report_escapes_user_content(client, seeded):
    h = seeded["headers"]
    r = submit(client, seeded, name="<script>alert(1)</script>")
    run_pending_jobs()
    exp_id = r.json()["experiment"]["id"]
    body = client.get(f"/api/v1/experiments/{exp_id}/report", headers=h).text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_assistant_answers_from_evidence_and_declines_advice(client, seeded):
    h = seeded["headers"]
    exp_id = _completed_experiment(client, seeded)
    r = client.post(
        f"/api/v1/experiments/{exp_id}/assistant",
        headers=h,
        json={"question": "What was the max drawdown and Sharpe ratio?"},
    )
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["provider"] == "deterministic"  # no LLM key configured in tests
    assert a["verification"]["grounded"] is True
    assert a["facts"]
    assert {f["id"] for f in a["facts"]} == set(a["citations"])
    exp = client.get(f"/api/v1/experiments/{exp_id}", headers=h).json()
    sharpe = next(f for f in a["facts"] if f["label"] == "Sharpe ratio")
    assert abs(sharpe["value"] - exp["summary"]["sharpe"]) < 1e-12  # same number as analytics
    # Synthetic data, so the answer says so, whatever was asked (#230).
    assert a["synthetic"] is True and "synthetic data" in a["text"].lower()
    adv = client.post(
        f"/api/v1/experiments/{exp_id}/assistant",
        headers=h,
        json={"question": "Should I invest in this strategy?"},
    ).json()
    assert adv["declined"] is True
    assert adv["synthetic"] is True and "synthetic data" in adv["text"].lower()
    assert adv["citations"] == []
    bad = client.post(f"/api/v1/experiments/{exp_id}/assistant", headers=h, json={"question": ""})
    assert bad.status_code == 422


def test_validating_an_older_engines_experiment_labels_the_mismatch(client, seeded):
    """#155: the suite re-runs backtests on the worker's current engine. An experiment
    computed on an older engine is validated under different rules, and says so."""
    import uuid

    from quantlab.backtest.engine import ENGINE_VERSION
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Experiment

    h = seeded["headers"]
    exp_id = _completed_experiment(client, seeded)
    with SessionLocal() as db:  # as if it had been computed before #150
        db.get(Experiment, uuid.UUID(exp_id)).engine_version = "1.3.0"
        db.commit()
    assert client.post(f"/api/v1/experiments/{exp_id}/validation", headers=h).status_code == 202
    assert run_pending_jobs() == ["completed"]
    val = client.get(f"/api/v1/experiments/{exp_id}/validation", headers=h).json()
    assert val["engine_version"] == ENGINE_VERSION  # the engine it actually ran on
    assert val["result"]["experiment_engine_version"] == "1.3.0"
    # A flag the web, reports and the assistant can read without parsing text.
    assert val["comparable_engine"] is False and val["result"]["comparable_engine"] is False
    rep = client.get(f"/api/v1/experiments/{exp_id}/integrity", headers=h).json()
    check = {c["key"]: c for c in rep["checks"]}["validation_engine"]
    assert check["status"] == "warning"
    assert check["finding"] == (
        f"The validation ran on engine {ENGINE_VERSION}; the experiment was computed on "
        "engine 1.3.0. These results are not directly comparable under identical engine "
        f"rules. Reproduce the experiment on engine {ENGINE_VERSION} and validate the "
        "reproduction for a like-for-like comparison."
    )
    assert check["evidence"] == {
        "validation_engine_version": ENGINE_VERSION,
        "experiment_engine_version": "1.3.0",
        "comparable_engine": False,
    }


def test_a_validation_from_before_155_is_not_reused_and_can_be_reassessed(client, seeded):
    """#156 review (a): a completed run of suite version 1 records no engine, and the
    per-experiment dedupe used to return it for good. The suite version is part of the
    run's params hash, so a new request now runs a fresh suite that records both."""
    import uuid

    from quantlab_api.db import SessionLocal
    from quantlab_api.jobs import payload_hash
    from quantlab_api.models import ResearchRun
    from quantlab_api.routes.research import SUITE, SUITE_VERSION

    assert SUITE_VERSION >= 2
    h = seeded["headers"]
    exp_id = _completed_experiment(client, seeded)
    old = client.post(f"/api/v1/experiments/{exp_id}/validation", headers=h).json()
    assert run_pending_jobs() == ["completed"]
    old_params = {"suite_version": 1, "seed": 0}
    with SessionLocal() as db:  # rewrite it as a completed run recorded before #155
        row = db.get(ResearchRun, uuid.UUID(old["id"]))
        row.params, row.params_hash = old_params, payload_hash(SUITE, old_params)
        row.result = {
            k: v
            for k, v in row.result.items()
            if k not in ("engine_version", "experiment_engine_version", "comparable_engine")
        }
        db.commit()
    rep = client.get(f"/api/v1/experiments/{exp_id}/integrity", headers=h).json()
    check = {c["key"]: c for c in rep["checks"]}["validation_engine"]
    assert check["status"] == "unassessed" and "before #155" in check["finding"]

    new = client.post(f"/api/v1/experiments/{exp_id}/validation", headers=h)
    assert new.status_code == 202
    assert new.json()["id"] != old["id"] and new.json()["status"] == "queued"  # not reused
    assert run_pending_jobs() == ["completed"]
    current = client.get(f"/api/v1/experiments/{exp_id}/validation", headers=h).json()
    assert current["id"] == new.json()["id"] and current["comparable_engine"] is True
    rep = client.get(f"/api/v1/experiments/{exp_id}/integrity", headers=h).json()
    assert {c["key"]: c for c in rep["checks"]}["validation_engine"]["status"] == "verified"
