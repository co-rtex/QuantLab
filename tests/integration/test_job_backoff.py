"""A queued retry runs only once its backoff has passed, whatever the queue delivers (#123).

The backoff used to live only in the RQ scheduler's delay, so an early (or forged)
delivery ran a retry before it was due. The job row now records when it may run.
"""

from __future__ import annotations

from datetime import timedelta

import fakeredis
import pytest
from sqlalchemy import update

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
    J.set_redis(fakeredis.FakeRedis())
    yield calls
    J.HANDLERS.pop("t.backoff", None)
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


def test_an_early_delivery_does_not_run_a_retry_and_a_due_one_does(flaky):
    jid = _make_job("t.backoff")
    assert J.execute_job(str(jid)) == "retry"
    job = _row(jid)
    assert job.status == JobStatus.queued
    assert job.not_before is not None and job.not_before > J.now()
    scheduled = len(J.get_queue().scheduled_job_registry)

    # Delivered before the backoff ends: nothing runs, no attempt is spent...
    assert J.execute_job(str(jid)) == "skipped"
    job = _row(jid)
    assert (job.status, job.attempts, flaky["n"]) == (JobStatus.queued, 1, 1)
    # ...and a delivery is scheduled for when it is due, so a fast clock cannot strand it.
    assert len(J.get_queue().scheduled_job_registry) == scheduled + 1

    # Once due, the delivery claims it.
    _set(jid, not_before=J.now() - timedelta(seconds=1))
    assert J.execute_job(str(jid)) == "completed"
    job = _row(jid)
    assert (job.status, job.attempts, job.not_before) == (JobStatus.completed, 2, None)


def test_the_reaper_does_not_redeliver_a_retry_before_it_is_due(flaky):
    jid = _make_job("t.backoff")
    long_ago = J.now() - timedelta(hours=1)
    _set(jid, created_at=long_ago, heartbeat_at=None, not_before=J.now() + timedelta(minutes=5))
    assert J.reap()["redelivered"] == 0

    _set(jid, not_before=J.now() - timedelta(seconds=1))
    assert J.reap()["redelivered"] == 1


def test_a_delayed_retry_can_still_be_cancelled(flaky):
    jid = _make_job("t.backoff")
    assert J.execute_job(str(jid)) == "retry"
    with SessionLocal() as db:
        job = db.get(Job, jid)
        assert job is not None and job.not_before is not None
        J.request_cancel(db, job)
    assert _row(jid).status == JobStatus.cancelled
