"""Failure injection for the job system: duplicates, crashes, retries, cancel, timeouts."""

from __future__ import annotations

import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from quantlab_api import jobs as J
from quantlab_api.db import SessionLocal
from quantlab_api.models import Experiment, ExperimentResult, Job, JobStatus, User, Workspace
from tests.integration.conftest import run_pending_jobs
from tests.integration.test_api_workflow import submit


def _job(db, exp_id):
    return db.scalar(select(Job).where(Job.payload["experiment_id"].as_string() == exp_id))


def _results(db):
    return db.scalar(select(func.count()).select_from(ExperimentResult))


def test_duplicate_delivery_commits_exactly_one_result(client, seeded):
    exp_id = submit(client, seeded).json()["experiment"]["id"]
    with SessionLocal() as db:
        job_id = str(_job(db, exp_id).id)
    assert J.execute_job(job_id, "w1") == "completed"
    assert J.execute_job(job_id, "w2") == "skipped"  # redelivered message is a no-op
    with SessionLocal() as db:
        assert _results(db) == 1
        assert db.get(Job, uuid.UUID(job_id)).attempts == 1


def test_crashed_worker_lease_expiry_then_takeover_is_fenced(client, seeded):
    exp_id = submit(client, seeded).json()["experiment"]["id"]
    with SessionLocal() as db:
        job_id = _job(db, exp_id).id
    # Worker A claims and then "freezes" (never heartbeats again).
    a = J.claim(job_id, "worker-A")
    assert a is not None
    assert a.attempts == 1
    ctx_a = J.JobContext(
        job_id=job_id,
        kind="backtest",
        attempt=1,
        worker_id="worker-A",
        timeout_seconds=900,
        payload=a.payload,
    )
    with SessionLocal() as db:
        db.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(lease_expires_at=func.now() - timedelta(seconds=1))
        )
        db.commit()
    # A redelivery while the lease is expired lets worker B take over (attempt 2).
    assert J.execute_job(str(job_id), "worker-B") == "completed"
    # Worker A wakes up: its fencing token is stale, so it can neither heartbeat nor commit.
    with pytest.raises(J.LeaseLostError):
        ctx_a.heartbeat(force=True)
    with SessionLocal() as db:
        assert _results(db) == 1
        job = db.get(Job, job_id)
        assert job.status == JobStatus.completed
        assert job.attempts == 2
        assert job.worker_id == "worker-B"


def test_reaper_requeues_then_fails_exhausted_jobs(client, seeded):
    exp_id = submit(client, seeded).json()["experiment"]["id"]
    with SessionLocal() as db:
        job_id = _job(db, exp_id).id
    for expected_attempt in (1, 2, 3):
        claimed = J.claim(job_id, f"dead-{expected_attempt}")
        assert claimed.attempts == expected_attempt
        with SessionLocal() as db:
            db.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(lease_expires_at=func.now() - timedelta(seconds=1))
            )
            db.commit()
        stats = J.reap()
        if expected_attempt < 3:
            assert stats["requeued"] == 1
        else:
            assert stats["failed"] == 1
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.status == JobStatus.failed
        assert job.error["type"] == "LeaseExpired"
        assert db.get(Experiment, uuid.UUID(exp_id)).status == JobStatus.failed
        assert _results(db) == 0


def test_reaper_recovers_and_job_completes(client, seeded):
    exp_id = submit(client, seeded).json()["experiment"]["id"]
    with SessionLocal() as db:
        job_id = _job(db, exp_id).id
    J.claim(job_id, "dead-worker")
    with SessionLocal() as db:
        db.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(lease_expires_at=func.now() - timedelta(seconds=1))
        )
        db.commit()
    assert J.reap()["requeued"] == 1
    assert run_pending_jobs() == ["completed"]


def _make_job(kind: str, timeout: int = 900, max_attempts: int = 3) -> uuid.UUID:
    with SessionLocal() as db:
        user = db.scalar(select(User))
        ws = db.scalar(select(Workspace))
        job, _ = J.submit_job(
            db,
            workspace_id=ws.id,
            user_id=user.id,
            kind=kind,
            payload={},
            timeout_seconds=timeout,
            dispatch=False,
        )
        if max_attempts != 3:
            job.max_attempts = max_attempts
            db.commit()
        return job.id


@pytest.fixture
def test_handlers(seeded):
    calls = {"flaky": 0}

    def flaky(ctx):
        calls["flaky"] += 1
        if calls["flaky"] < 3:
            raise ConnectionError("transient")
        return lambda db: {"message": "ok after retries"}

    def permanent(ctx):
        raise J.PermanentJobError("bad input")

    def cancellable(ctx):
        with SessionLocal() as db:
            db.execute(update(Job).where(Job.id == ctx.job_id).values(cancel_requested=True))
            db.commit()
        for _ in range(5):
            ctx.heartbeat(force=True)
        return lambda db: None

    def slow(ctx):
        time.sleep(1.2)
        ctx.heartbeat(force=True)
        return lambda db: None

    J.HANDLERS.update(
        {"t.flaky": flaky, "t.permanent": permanent, "t.cancel": cancellable, "t.slow": slow}
    )
    yield calls
    for k in ("t.flaky", "t.permanent", "t.cancel", "t.slow"):
        J.HANDLERS.pop(k, None)


def _due(jid):
    """Let a queued retry's backoff pass at once (the queue's delay is skipped here)."""
    with SessionLocal() as db:
        db.execute(update(Job).where(Job.id == jid).values(not_before=None))
        db.commit()


def test_transient_errors_retry_until_success(test_handlers):
    jid = _make_job("t.flaky")
    assert J.execute_job(str(jid)) == "retry"
    with SessionLocal() as db:
        job = db.get(Job, jid)
        assert job.status == JobStatus.queued
        assert job.error["retryable"] is True
    _due(jid)
    assert J.execute_job(str(jid)) == "retry"
    _due(jid)
    assert J.execute_job(str(jid)) == "completed"
    with SessionLocal() as db:
        job = db.get(Job, jid)
        assert job.attempts == 3
        assert job.message == "ok after retries"


def test_permanent_errors_fail_without_retry(test_handlers):
    jid = _make_job("t.permanent")
    assert J.execute_job(str(jid)) == "failed"
    with SessionLocal() as db:
        job = db.get(Job, jid)
        assert job.status == JobStatus.failed
        assert job.attempts == 1
        assert job.error["type"] == "PermanentJobError"
        assert job.error["retryable"] is False
    assert J.execute_job(str(jid)) == "skipped"


def test_running_job_observes_cancellation(test_handlers):
    jid = _make_job("t.cancel")
    assert J.execute_job(str(jid)) == "cancelled"
    with SessionLocal() as db:
        assert db.get(Job, jid).status == JobStatus.cancelled


def test_timeout_is_enforced(test_handlers):
    jid = _make_job("t.slow", timeout=1)
    assert J.execute_job(str(jid)) == "timed_out"
    with SessionLocal() as db:
        job = db.get(Job, jid)
        assert job.status == JobStatus.timed_out
        assert job.error["type"] == "JobTimeoutError"


def _under_rq_timeout(jid: uuid.UUID, seconds: int = 1) -> str:
    """Run the job as RQ's worker does, inside its job-timeout alarm (UnixSignalDeathPenalty,
    set at timeout + 60 s in production; ``seconds`` here)."""
    from rq.timeouts import JobTimeoutException, UnixSignalDeathPenalty

    with UnixSignalDeathPenalty(seconds, JobTimeoutException, job_id=str(jid)):
        return J.execute_job(str(jid))


def _overrun(ctx):
    time.sleep(5)  # a long phase without progress callbacks: no heartbeat, no ctx timeout
    return lambda db: {"message": "should not finish"}


def test_a_job_stopped_by_rq_s_own_timeout_is_timed_out_not_retried(test_handlers, monkeypatch):
    """#212: RQ's alarm raised rq.timeouts.JobTimeoutException in the handler, which was
    taken for a retryable error: the job ran its whole timeout again, up to max_attempts,
    and was then reported failed."""
    monkeypatch.setitem(J.HANDLERS, "t.overrun", _overrun)
    jid = _make_job("t.overrun", timeout=10)
    assert _under_rq_timeout(jid) == "timed_out"
    with SessionLocal() as db:
        job = db.get(Job, jid)
        assert (job.status, job.attempts) == (JobStatus.timed_out, 1)
        assert job.error["type"] == "JobTimeoutError"
        assert job.message == "exceeded 10s"
        assert job.lease_expires_at is None and job.not_before is None
    assert J.execute_job(str(jid)) == "skipped"  # never claimed again


def test_a_backtest_stopped_by_rq_s_timeout_marks_its_experiment_timed_out(
    client, seeded, monkeypatch
):
    monkeypatch.setitem(J.HANDLERS, "backtest", _overrun)
    exp_id = submit(client, seeded).json()["experiment"]["id"]
    with SessionLocal() as db:
        jid = _job(db, exp_id).id
    assert _under_rq_timeout(jid) == "timed_out"
    with SessionLocal() as db:
        assert db.get(Job, jid).status == JobStatus.timed_out
        assert db.get(Experiment, uuid.UUID(exp_id)).status == JobStatus.timed_out
        assert _results(db) == 0


def test_failed_backtest_leaves_no_partial_results(client, seeded, monkeypatch):
    exp_id = submit(client, seeded).json()["experiment"]["id"]
    from quantlab_api import tasks

    def boom(*a, **k):
        raise J.PermanentJobError("simulated failure after simulation")

    monkeypatch.setattr(tasks, "analyze_backtest", boom)
    assert run_pending_jobs() == ["failed"]
    from quantlab_api.artifacts import experiment_dir

    with SessionLocal() as db:
        assert _results(db) == 0
        assert db.get(Experiment, uuid.UUID(exp_id)).status == JobStatus.failed
    assert not experiment_dir(exp_id).exists()
    r = client.get(f"/api/v1/experiments/{exp_id}/equity", headers=seeded["headers"])
    assert r.status_code == 409


def test_cancel_running_backtest_via_api(client, seeded, monkeypatch):
    """Cancellation requested mid-simulation stops the engine via the progress heartbeat."""
    h = seeded["headers"]
    sub = submit(client, seeded, strategy="equal_weight", params={}).json()
    job_id = sub["job"]["id"]
    from quantlab_api import tasks

    original = tasks.run_backtest

    def run_and_cancel(*args, **kwargs):
        client.post(f"/api/v1/jobs/{job_id}/cancel", headers=h)
        return original(*args, **kwargs)

    monkeypatch.setattr(tasks, "run_backtest", run_and_cancel)
    original_hb = J.JobContext.heartbeat

    def eager(self, progress=None, message=None, force=False):
        return original_hb(self, progress, message, force=True)

    monkeypatch.setattr(J.JobContext, "heartbeat", eager)
    assert run_pending_jobs() == ["cancelled"]
    exp = client.get(f"/api/v1/experiments/{sub['experiment']['id']}", headers=h).json()
    assert exp["status"] == "cancelled"
    assert exp["summary"] is None


def test_redis_outage_does_not_lose_jobs(client, seeded, monkeypatch):
    class Down:
        def __getattr__(self, name):
            raise ConnectionError("redis down")

    J.set_redis(Down())  # type: ignore[arg-type]
    r = submit(client, seeded)
    assert r.status_code == 202  # accepted and durably recorded despite the outage
    with SessionLocal() as db:
        job = _job(db, r.json()["experiment"]["id"])
        assert job.status == JobStatus.queued
    import fakeredis

    J.set_redis(fakeredis.FakeRedis())
    with SessionLocal() as db:
        db.execute(update(Job).values(created_at=func.now() - timedelta(minutes=10)))
        db.commit()
    assert J.reap()["redelivered"] == 1
    assert len(J.get_queue()) == 1
    # Later cycles must not pile duplicates onto the queue while the job is still waiting.
    for _ in range(3):
        assert J.reap()["redelivered"] == 0
    assert len(J.get_queue()) == 1
