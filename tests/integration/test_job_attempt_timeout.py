"""``timeout_seconds`` applies to each attempt, by the time it ran (#167).

The reaper timed a stale job out from ``started_at``, attempt 1's start, kept across retries,
and up to its own run: waiting for a worker between attempts, and the time since a worker
died, both counted. A job requeued after a long outage was timed out at once instead of
retried. All times here are database times (#130).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from alembic import command
from sqlalchemy import create_engine, func, inspect, select, update

from quantlab_api import jobs as J
from quantlab_api.db import SessionLocal
from quantlab_api.models import Experiment, Job, JobStatus
from tests.integration.conftest import run_pending_jobs
from tests.integration.test_api_workflow import submit
from tests.integration.test_migrations import BASE_URL, _cfg


def _backtest(client, seeded) -> tuple[uuid.UUID, uuid.UUID]:
    exp_id = uuid.UUID(submit(client, seeded).json()["experiment"]["id"])
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(Job.payload["experiment_id"].as_string() == str(exp_id)))
        assert job.timeout_seconds == 900
        return job.id, exp_id


def _ago(seconds: float):
    return func.now() - timedelta(seconds=seconds)


def _set(job_id: uuid.UUID, **values) -> None:
    with SessionLocal() as db:
        db.execute(update(Job).where(Job.id == job_id).values(**values))
        db.commit()


def test_each_claim_records_its_own_start_and_keeps_the_first(client, seeded):
    job_id, _ = _backtest(client, seeded)
    J.claim(job_id, "w1")
    _set(
        job_id,
        started_at=_ago(3600),
        attempt_started_at=_ago(3600),
        heartbeat_at=_ago(3570),
        lease_expires_at=_ago(3510),
    )
    assert J.reap()["requeued"] == 1
    J.claim(job_id, "w2")
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        now = db.scalar(select(func.now()))
        assert job.attempts == 2
        assert (now - job.started_at).total_seconds() > 3500  # first started, kept
        assert (now - job.attempt_started_at).total_seconds() < 60  # this attempt
    out = client.get(f"/api/v1/jobs/{job_id}", headers=seeded["headers"]).json()
    assert out["attempt_started_at"] is not None and out["started_at"] < out["attempt_started_at"]


def test_a_requeue_after_a_long_gap_is_not_timed_out_and_attempt_2_completes(client, seeded):
    """Attempt 1's worker died 30 s into it, two hours ago, and nothing ran since (the
    reaper too was down). Two hours is far past the 900 s timeout, but the attempt ran at most 90 s (to its lease's end)."""
    job_id, exp_id = _backtest(client, seeded)
    J.claim(job_id, "lost-worker")
    _set(
        job_id,
        started_at=_ago(7200),
        attempt_started_at=_ago(7200),
        heartbeat_at=_ago(7170),
        lease_expires_at=_ago(7110),
    )
    stats = J.reap()
    assert stats["requeued"] == 1 and stats["timed_out"] == 0
    assert run_pending_jobs() == ["completed"]
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.status == JobStatus.completed and job.attempts == 2
        assert db.get(Experiment, exp_id).status == JobStatus.completed


def test_an_attempt_2_that_loses_its_lease_after_a_long_gap_is_retried(client, seeded):
    """The gap is between attempts: attempt 1 was lost two hours ago, attempt 2 started
    now and missed its lease. Timed from attempt 1's start it was timed out at once."""
    job_id, _ = _backtest(client, seeded)
    J.claim(job_id, "lost-worker")
    _set(
        job_id,
        started_at=_ago(7200),
        attempt_started_at=_ago(7200),
        heartbeat_at=_ago(7200),
        lease_expires_at=_ago(7140),
    )
    assert J.reap()["requeued"] == 1
    J.claim(job_id, "worker-2")
    _set(job_id, lease_expires_at=_ago(1))
    stats = J.reap()
    assert stats["requeued"] == 1 and stats["timed_out"] == 0
    assert run_pending_jobs() == ["completed"]
    with SessionLocal() as db:
        assert db.get(Job, job_id).attempts == 3


def test_an_attempt_that_itself_ran_past_the_timeout_is_still_timed_out(client, seeded):
    job_id, exp_id = _backtest(client, seeded)
    J.claim(job_id, "stuck-worker")
    # Claimed 2000 s ago, heartbeating until 30 s ago, lease out 1 s ago: 1999 s, past 900 s.
    _set(job_id, attempt_started_at=_ago(2000), heartbeat_at=_ago(30), lease_expires_at=_ago(1))
    stats = J.reap()
    assert stats["timed_out"] == 1 and stats["requeued"] == 0
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.status == JobStatus.timed_out
        assert db.get(Experiment, exp_id).status == JobStatus.timed_out
    assert run_pending_jobs() == []


def test_attempt_2_is_timed_from_its_own_claim_after_waiting_two_hours_queued(client, seeded):
    """Only the start differs from attempt 1's: attempt 1 was requeued promptly two hours
    ago, the job then waited queued (no worker), and attempt 2's lease just lapsed with the
    reaper running on time. Timed from started_at it would be timed out."""
    job_id, _ = _backtest(client, seeded)
    J.claim(job_id, "lost-worker")
    _set(  # as the reaper left it two hours ago
        job_id,
        status=JobStatus.queued,
        started_at=_ago(7200),
        attempt_started_at=_ago(7200),
        heartbeat_at=_ago(7170),
        lease_expires_at=None,
    )
    assert J.claim(job_id, "worker-2").attempts == 2
    _set(job_id, lease_expires_at=_ago(1))
    stats = J.reap()
    assert stats["requeued"] == 1 and stats["timed_out"] == 0


def test_an_overrun_killed_between_long_lease_heartbeats_is_timed_out(client, seeded, monkeypatch):
    """With a 300 s lease, keepalive beats every 100 s. RQ kills an attempt overrunning its
    900 s at 960 s, so its last beat can be at 900 s, not past the timeout. The lease then
    runs to 1200 s, which is past it: timed out, as before #167."""
    settings = J.get_settings()
    monkeypatch.setattr(settings, "job_lease_seconds", 300)
    job_id, exp_id = _backtest(client, seeded)
    J.claim(job_id, "overrunning-worker")
    # The reaper's view once the lease ran out: claimed 1201 s ago, last beat 900 s in.
    _set(
        job_id,
        attempt_started_at=_ago(1201),
        heartbeat_at=_ago(301),
        lease_expires_at=_ago(1),
    )
    stats = J.reap()
    assert stats["timed_out"] == 1 and stats["requeued"] == 0
    with SessionLocal() as db:
        assert db.get(Job, job_id).status == JobStatus.timed_out
        assert db.get(Experiment, exp_id).status == JobStatus.timed_out


def test_a_row_from_before_the_column_is_timed_from_its_first_start(client, seeded):
    job_id, _ = _backtest(client, seeded)
    J.claim(job_id, "old-worker")
    _set(
        job_id,
        attempt_started_at=None,
        started_at=_ago(2000),
        heartbeat_at=_ago(30),
        lease_expires_at=_ago(1),
    )
    assert J.reap()["timed_out"] == 1


def test_the_migration_adds_and_removes_only_the_column():
    cfg = _cfg()
    eng = create_engine(BASE_URL)
    try:
        command.downgrade(cfg, "4d2b8e6f0c15")
        cols = {c["name"] for c in inspect(eng).get_columns("jobs")}
        assert "attempt_started_at" not in cols and "started_at" in cols
        command.upgrade(cfg, "head")
        col = {c["name"]: c for c in inspect(eng).get_columns("jobs")}["attempt_started_at"]
        assert col["nullable"] is True
        command.check(cfg)
    finally:
        command.upgrade(cfg, "head")
        eng.dispose()
