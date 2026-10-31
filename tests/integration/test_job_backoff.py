"""A queued retry runs only once its backoff has passed, whatever the queue delivers (#123).

The backoff used to live only in the RQ scheduler's delay, so an early (or forged)
delivery ran a retry before it was due. The job row now records when it may run.
"""

from __future__ import annotations

from datetime import timedelta

import fakeredis
import pytest
from sqlalchemy import func, select, update

from quantlab_api import jobs as J
from quantlab_api.db import SessionLocal
from quantlab_api.models import Job, JobStatus
from tests.integration.test_jobs_reliability import _make_job


@pytest.fixture
def flaky(seeded):
    calls = {"n": 0}

    def handler(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("transient")
        return lambda db: {"message": "ok"}

    J.HANDLERS["t.backoff"] = handler
    J.HANDLERS["t.ok"] = lambda ctx: lambda db: {"message": "ok"}
    J.set_redis(fakeredis.FakeRedis())
    yield calls
    J.HANDLERS.pop("t.backoff", None)
    J.HANDLERS.pop("t.ok", None)
    J.set_redis(None)


def _row(jid) -> Job:
    with SessionLocal() as db:
        job = db.get(Job, jid)
        assert job is not None
        return job


def _set(jid, **values) -> None:
    with SessionLocal() as db:
        db.execute(update(Job).where(Job.id == jid).values(**values))
        db.commit()


def _db_now():
    """Deadlines are the database's time (#130), so tests set and compare them in it."""
    with SessionLocal() as db:
        return db.scalar(select(func.now()))


def test_an_early_delivery_does_not_run_a_retry_and_a_due_one_does(flaky):
    jid = _make_job("t.backoff")
    assert J.execute_job(str(jid)) == "retry"
    job = _row(jid)
    assert job.status == JobStatus.queued
    assert job.not_before is not None and job.not_before > _db_now()
    scheduled = len(J.get_queue().scheduled_job_registry)

    # Delivered before the backoff ends (twice): nothing runs, no attempt is spent...
    assert J.execute_job(str(jid)) == "skipped"
    assert J.execute_job(str(jid)) == "skipped"
    job = _row(jid)
    assert (job.status, job.attempts, flaky["n"]) == (JobStatus.queued, 1, 1)
    # ...and one delivery is scheduled for when it is due, however many came early.
    assert len(J.get_queue().scheduled_job_registry) == scheduled + 1

    # Once due, the delivery claims it.
    _set(jid, not_before=func.now() - timedelta(seconds=1))
    assert J.execute_job(str(jid)) == "completed"
    job = _row(jid)
    assert (job.status, job.attempts, job.not_before) == (JobStatus.completed, 2, None)


def test_the_reaper_does_not_redeliver_a_retry_before_it_is_due(flaky):
    jid = _make_job("t.backoff")
    long_ago = func.now() - timedelta(hours=1)
    _set(jid, created_at=long_ago, heartbeat_at=None, not_before=func.now() + timedelta(minutes=5))
    assert J.reap()["redelivered"] == 0

    _set(jid, not_before=func.now() - timedelta(seconds=1))
    assert J.reap()["redelivered"] == 1


def test_a_delayed_retry_can_still_be_cancelled_and_then_never_runs(flaky):
    jid = _make_job("t.backoff")
    assert J.execute_job(str(jid)) == "retry"
    with SessionLocal() as db:
        job = db.get(Job, jid)
        assert job is not None and job.not_before is not None
        J.request_cancel(db, job)
    assert _row(jid).status == JobStatus.cancelled
    # Its pending deliveries still arrive, before and after the due time: none runs it.
    assert J.execute_job(str(jid)) == "skipped"
    _set(jid, not_before=func.now() - timedelta(seconds=1))
    assert J.execute_job(str(jid)) == "skipped"
    assert _row(jid).status == JobStatus.cancelled
    assert flaky["n"] == 1


def test_a_job_whose_worker_was_lost_is_due_at_once(flaky):
    """Lease loss is not a backoff: the reaper requeues the job, and it runs on the next
    delivery. claim() clears not_before, so a running row never carries one."""
    jid = _make_job("t.ok")
    claimed = J.claim(jid, "w1")
    assert claimed is not None and claimed.not_before is None
    _set(jid, lease_expires_at=func.now() - timedelta(seconds=1))  # w1 stopped heartbeating
    assert J.reap()["requeued"] == 1
    job = _row(jid)
    assert (job.status, job.not_before) == (JobStatus.queued, None)
    assert J.execute_job(str(jid), "w2") == "completed"
    assert _row(jid).attempts == 2
