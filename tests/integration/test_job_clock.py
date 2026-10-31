"""Job timing uses the database's clock, not each host's (#130).

A worker writes a deadline (a lease, a retry's backoff) and another worker or the reaper
compares it. With each host's own clock, a host running fast claimed a retry early or
requeued a live job, and one running slow wrote a lease that was already expired, so a
second worker took the job over at once. Each test moves this host's clock by an hour
(``jobs.now``) while the database keeps real time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from sqlalchemy import func, select, update

from quantlab_api import jobs as J
from quantlab_api.db import SessionLocal
from quantlab_api.models import Job, JobStatus
from tests.integration.test_jobs_reliability import _make_job

HOUR = 3600


@pytest.fixture
def host_clock(seeded, monkeypatch):
    """Set this host's clock ``seconds`` away from the database's."""
    J.set_redis(fakeredis.FakeRedis())
    calls = {"n": 0}

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("transient")
        return lambda db: {"message": "ok"}

    J.HANDLERS["t.clock"] = flaky

    def offset(seconds: float) -> None:
        monkeypatch.setattr(J, "now", lambda: datetime.now(UTC) + timedelta(seconds=seconds))

    yield offset
    J.HANDLERS.pop("t.clock", None)
    J.set_redis(None)


def _db_now() -> datetime:
    with SessionLocal() as db:
        t = db.scalar(select(func.now()))
        assert t is not None
        return t


def _row(jid) -> Job:
    with SessionLocal() as db:
        job = db.get(Job, jid)
        assert job is not None
        return job


def test_a_fast_host_does_not_claim_a_retry_early(host_clock):
    jid = _make_job("t.clock")
    with SessionLocal() as db:
        db.execute(
            update(Job).where(Job.id == jid).values(not_before=func.now() + timedelta(seconds=60))
        )
        db.commit()
    host_clock(+HOUR)
    assert J.claim(jid, "fast") is None
    assert _row(jid).status == JobStatus.queued


def test_a_retry_from_a_fast_host_waits_its_backoff_in_database_time(host_clock):
    jid = _make_job("t.clock")
    host_clock(+HOUR)
    assert J.execute_job(str(jid), "fast") == "retry"
    wait = (_row(jid).not_before - _db_now()).total_seconds()
    assert 0 < wait <= 2.0**1 + 1  # the first backoff (2 s), not an hour away


def test_a_slow_host_does_not_write_an_expired_lease(host_clock):
    jid = _make_job("t.clock")
    host_clock(-HOUR)
    assert J.claim(jid, "slow") is not None
    host_clock(0)
    assert _row(jid).lease_expires_at > _db_now()
    assert J.claim(jid, "other") is None  # not taken over while the first worker holds it


def test_a_fast_reaper_does_not_requeue_a_live_job(host_clock):
    jid = _make_job("t.clock")
    assert J.claim(jid, "w1") is not None
    host_clock(+HOUR)
    assert J.reap()["requeued"] == 0
    assert _row(jid).status == JobStatus.running


def test_an_early_delivery_does_not_overwrite_itself(host_clock):
    """A delivery named for the job's due time that arrives early reschedules under another
    name: under its own, it would overwrite the entry being run, which RQ then marks
    finished, and the retry waits for the reaper (review of #129)."""
    from rq import SimpleWorker
    from rq.job import JobStatus as RQStatus
    from rq.serializers import JSONSerializer

    jid = _make_job("t.clock")
    with SessionLocal() as db:
        db.execute(
            update(Job).where(Job.id == jid).values(not_before=func.now() + timedelta(seconds=60))
        )
        db.commit()
    due = _row(jid).not_before
    queue = J.get_queue()
    queue.enqueue(J.execute_job, str(jid), job_id=f"{jid}-{round(due.timestamp() * 1_000_000)}")
    SimpleWorker([queue], connection=J.redis_conn(), **J.RQ_OPTIONS).work(burst=True)  # type: ignore[arg-type]

    scheduled = queue.scheduled_job_registry.get_job_ids()
    statuses = [
        J.TrustedJob.fetch(i, connection=J.redis_conn(), serializer=JSONSerializer).get_status()
        for i in scheduled
    ]
    assert RQStatus.SCHEDULED in statuses
    assert _row(jid).status == JobStatus.queued
