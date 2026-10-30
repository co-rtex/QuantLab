"""Regression tests for findings from the independent service-layer review."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select, update

from quantlab_api import jobs as J
from quantlab_api.db import SessionLocal
from quantlab_api.models import Experiment, Job, JobStatus
from tests.integration.conftest import register, run_pending_jobs
from tests.integration.test_api_workflow import submit


def _job_for(exp_id: str) -> Job:
    with SessionLocal() as db:
        return db.scalar(select(Job).where(Job.payload["experiment_id"].as_string() == exp_id))


def test_cancel_of_a_running_job_survives_worker_death(client, seeded):
    exp_id = submit(client, seeded).json()["experiment"]["id"]
    job = _job_for(exp_id)
    J.claim(job.id, "doomed-worker")
    client.post(f"/api/v1/jobs/{job.id}/cancel", headers=seeded["headers"])
    with SessionLocal() as db:  # the worker dies before its next heartbeat
        db.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(lease_expires_at=J.now() - timedelta(seconds=1))
        )
        db.commit()
    J.reap()
    with SessionLocal() as db:
        assert db.get(Job, job.id).status == JobStatus.cancelled
        assert db.get(Experiment, uuid.UUID(exp_id)).status == JobStatus.cancelled


def test_stuck_queued_cancelled_jobs_are_finalized(client, seeded):
    exp_id = submit(client, seeded).json()["experiment"]["id"]
    job = _job_for(exp_id)
    with SessionLocal() as db:  # e.g. requeued by a retry after cancel was requested
        db.execute(update(Job).where(Job.id == job.id).values(cancel_requested=True))
        db.commit()
    J.reap()
    with SessionLocal() as db:
        assert db.get(Job, job.id).status == JobStatus.cancelled


def test_job_errors_do_not_leak_tracebacks_or_paths(client, seeded, monkeypatch):
    exp_id = submit(client, seeded).json()["experiment"]["id"]
    from quantlab_api import tasks

    def boom(*a, **k):
        raise RuntimeError("psycopg: SELECT secret FROM somewhere /srv/private/path")

    monkeypatch.setattr(tasks, "analyze_backtest", boom)
    for _ in range(3):
        run_pending_jobs()
    j = client.get(f"/api/v1/jobs/{_job_for(exp_id).id}", headers=seeded["headers"]).json()
    assert j["status"] == "failed"
    assert "trace" not in j["error"]
    assert "/srv/private" not in j["error"]["message"]
    assert j["error"]["type"] == "RuntimeError"
    assert "/srv/private" not in j["message"]


def test_retry_mirrors_queued_status(client, seeded, monkeypatch):
    exp_id = submit(client, seeded).json()["experiment"]["id"]
    from quantlab_api import tasks

    monkeypatch.setattr(
        tasks, "analyze_backtest", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("blip"))
    )
    assert run_pending_jobs(max_jobs=1) == ["retry"]
    with SessionLocal() as db:
        assert db.get(Experiment, uuid.UUID(exp_id)).status == JobStatus.queued


def test_sole_owner_cannot_demote_themselves(client):
    h = register(client, "solo@example.com")
    ws = client.get("/api/v1/workspaces", headers=h).json()[0]["id"]
    r = client.post(
        f"/api/v1/workspaces/{ws}/members",
        headers=h,
        json={"email": "solo@example.com", "role": "viewer"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "last_owner"


def test_out_of_range_dates_are_422_not_500(client, seeded):
    h = seeded["headers"]
    exp_id = submit(client, seeded).json()["experiment"]["id"]
    run_pending_jobs()
    assert (
        client.get(f"/api/v1/experiments/{exp_id}/positions?on=0001-01-01", headers=h).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/v1/dataset-versions/{seeded['dv']}/bars/SYN001?start=0001-01-01", headers=h
        ).status_code
        == 422
    )
    r = client.post(
        f"/api/v1/workspaces/{seeded['ws']}/portfolios",
        headers=h,
        json={
            "name": "x",
            "dataset_version_id": seeded["dv"],
            "initial_deposit": 10,
            "start_date": "9999-12-31",
        },
    )
    assert r.status_code == 422
    bad = client.post(
        f"/api/v1/workspaces/{seeded['ws']}/experiments",
        headers=h,
        json={
            "name": "x",
            "dataset_version_id": seeded["dv"],
            "strategy_key": "equal_weight",
            "config": {"start": "0001-01-02"},
        },
    )
    assert bad.status_code == 422


def test_validation_key_reuse_after_failure_does_not_wedge_the_run(client, seeded, monkeypatch):
    h = seeded["headers"]
    exp_id = submit(client, seeded).json()["experiment"]["id"]
    run_pending_jobs()
    from quantlab.research import suite

    monkeypatch.setattr(
        suite,
        "run_validation_suite",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("bad input")),
    )
    from quantlab_api import tasks  # noqa: F401  (handler imports suite lazily)

    k = {**h, "Idempotency-Key": "val-1"}
    first = client.post(f"/api/v1/experiments/{exp_id}/validation", headers=k).json()
    run_pending_jobs()
    replay = client.post(f"/api/v1/experiments/{exp_id}/validation", headers=k).json()
    assert replay["id"] == first["id"]
    assert replay["status"] == "failed"  # reported as-is, not reset to a dead "queued"
    monkeypatch.undo()
    fresh = client.post(f"/api/v1/experiments/{exp_id}/validation", headers=h).json()
    assert fresh["status"] == "queued"
    assert run_pending_jobs() == ["completed"]


def test_active_job_cap(client, seeded, monkeypatch):
    from quantlab_api.config import get_settings

    monkeypatch.setattr(get_settings(), "max_active_jobs_per_workspace", 2)
    codes = [submit(client, seeded, name=f"cap-{i}").status_code for i in range(3)]
    assert codes == [202, 202, 429]
