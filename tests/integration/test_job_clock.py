"""Job timing uses the database's clock, not each host's (#130).

A worker writes a deadline (a lease, a retry's backoff) and another worker or the reaper
compares it. With each host's own clock, a host running fast claimed a retry early or
requeued a live job, and one running slow wrote a lease that was already expired, so a
second worker took the job over at once. Each test moves this host's clock by an hour
(``datetime`` as the ``jobs`` and ``models`` modules see it, so a direct ``datetime.now()``
moves too) while the database keeps real time.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import fakeredis
import pytest
from sqlalchemy import func, select, update

from quantlab_api import jobs as J
from quantlab_api import models as M
from quantlab_api.db import SessionLocal
from quantlab_api.models import Job, JobStatus, User, Workspace
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
    J.HANDLERS["t.ok"] = lambda ctx: lambda db: {"message": "ok"}

    def offset(seconds: float) -> None:
        shift = timedelta(seconds=seconds)

        class Skewed(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[no-untyped-def, override]
                return datetime.now(tz) + shift

        monkeypatch.setattr(J, "datetime", Skewed)
        monkeypatch.setattr(M, "datetime", Skewed)

    yield offset
    J.HANDLERS.pop("t.clock", None)
    J.HANDLERS.pop("t.ok", None)
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


def test_a_fast_api_host_does_not_delay_a_failed_first_delivery(host_clock, monkeypatch):
    """A job's creation time is the database's too: the reaper re-delivers a job whose first
    enqueue failed once *database* time passes created_at + 120 s. Written with an API host
    clock an hour fast, it waited an hour (review of #132)."""
    host_clock(+HOUR)  # the API host that submits the job

    def redis_down() -> None:
        raise ConnectionError("redis is down")

    with monkeypatch.context() as m:
        m.setattr(J, "get_queue", redis_down)
        with SessionLocal() as db:
            user = db.scalar(select(User))
            ws = db.scalar(select(Workspace))
            assert user is not None and ws is not None
            job, _ = J.submit_job(  # dispatches, and the enqueue fails
                db, workspace_id=ws.id, user_id=user.id, kind="t.ok", payload={}
            )
            jid = job.id
    host_clock(0)
    assert abs((_row(jid).created_at - _db_now()).total_seconds()) < 5
    assert J.reap()["redelivered"] == 0  # not before its 120 s
    with SessionLocal() as db:  # 121 s of database time pass
        db.execute(
            update(Job)
            .where(Job.id == jid)
            .values(created_at=Job.created_at - timedelta(seconds=121))
        )
        db.commit()
    assert J.reap()["redelivered"] == 1
    assert J.execute_job(str(jid)) == "completed"


def test_an_early_delivery_is_rescheduled_and_runs_once_due(host_clock, monkeypatch):
    """A delivery named for the job's due time that arrives early is put back for that time
    under another name ("-again"), and once due it reaches a worker and the job runs
    (reviews of #129 and #132)."""
    from rq import SimpleWorker
    from rq import scheduler as rq_scheduler
    from rq.serializers import JSONSerializer

    jid = _make_job("t.ok")
    with SessionLocal() as db:
        db.execute(
            update(Job).where(Job.id == jid).values(not_before=func.now() + timedelta(seconds=60))
        )
        db.commit()
    due = _row(jid).not_before
    named = f"{jid}-{round(due.timestamp() * 1_000_000)}"
    queue = J.get_queue()
    queue.enqueue(J.execute_job, str(jid), job_id=named)

    def work() -> None:
        SimpleWorker([queue], connection=J.redis_conn(), **J.RQ_OPTIONS).work(burst=True)  # type: ignore[arg-type]

    work()  # early: nothing runs, and the delivery is put back for the due time
    assert _row(jid).status == JobStatus.queued
    assert queue.scheduled_job_registry.get_job_ids() == [f"{named}-again"]

    # The due time passes, in the database and for RQ's scheduler.
    with SessionLocal() as db:
        db.execute(update(Job).where(Job.id == jid).values(not_before=func.now()))
        db.commit()
    later = rq_scheduler.current_timestamp() + 120
    monkeypatch.setattr(rq_scheduler, "current_timestamp", lambda: later)
    scheduler = rq_scheduler.RQScheduler(
        [queue.name], connection=J.redis_conn(), serializer=JSONSerializer
    )
    scheduler.prepare_registries([queue.name])  # as acquire_locks() would; no Lua in fakeredis
    scheduler.enqueue_scheduled_jobs()
    work()
    job = _row(jid)
    assert (job.status, job.attempts) == (JobStatus.completed, 1)
