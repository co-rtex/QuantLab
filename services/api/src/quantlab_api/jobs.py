"""Durable background jobs.

PostgreSQL is the source of truth for job state; Redis/RQ is only the delivery mechanism.
Delivery is therefore at-least-once and every step is designed to tolerate duplicates:

* **Submission** is idempotent per ``(workspace, Idempotency-Key)``.
* **Claiming** is a single atomic ``UPDATE ... RETURNING`` that only succeeds for a
  queued job or a running job whose lease has expired. The incremented ``attempts`` value
  is the fencing token for that execution.
* **Heartbeats** extend the lease while work progresses and observe cancellation and
  timeouts.
* **Commit** locks the job row, verifies the fencing token is still current, and writes
  results plus the terminal status in one transaction. A worker that lost its lease
  (e.g. paused, then resumed after another worker took over) cannot commit, and the
  unique constraint on results makes a second logical result impossible.
* **Recovery** (the reaper) requeues jobs whose lease expired and fails jobs that ran out
  of attempts or wall-clock time.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from redis import Redis
from rq import Queue
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from quantlab_api.config import get_settings
from quantlab_api.db import SessionLocal
from quantlab_api.errors import ApiError
from quantlab_api.models import TERMINAL, Experiment, Job, JobStatus, ResearchRun
from quantlab_api.observability import (
    JOB_OUTCOMES,
    JOB_QUEUE_WAIT_SECONDS,
    JOB_RETRIES,
    JOB_RUN_SECONDS,
    JOBS_RUNNING,
    log,
)


class JobCancelledError(Exception):
    pass


class JobTimeoutError(Exception):
    pass


class PermanentJobError(Exception):
    """Failure that retrying cannot fix (invalid input, missing data)."""


class LeaseLostError(Exception):
    pass


def now() -> datetime:
    return datetime.now(UTC)


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


# --------------------------------------------------------------------------- queueing
_redis: Redis | None = None  # type: ignore[type-arg]


def redis_conn() -> Redis:  # type: ignore[type-arg]
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url)
    return _redis


def set_redis(conn: Redis | None) -> None:  # type: ignore[type-arg]
    """Test hook: swap the Redis connection (e.g. for fakeredis)."""
    global _redis
    _redis = conn


def get_queue() -> Queue:
    return Queue(get_settings().queue_name, connection=redis_conn())


def enqueue(job: Job, delay_seconds: float = 0.0) -> bool:
    """Best effort: if Redis is down the job stays queued and the reaper re-delivers it."""
    try:
        q = get_queue()
        kwargs: dict[str, Any] = {
            "job_timeout": job.timeout_seconds + 60,
            "result_ttl": 3600,
            "failure_ttl": 86400,
            "description": f"{job.kind}:{job.id}",
        }
        if delay_seconds > 0:
            q.enqueue_in(timedelta(seconds=delay_seconds), execute_job, str(job.id), **kwargs)
        else:
            q.enqueue(execute_job, str(job.id), **kwargs)
        return True
    except Exception as exc:  # pragma: no cover - exercised in failure-injection tests
        log.warning("enqueue_failed", job_id=str(job.id), error=str(exc))
        return False


def payload_hash(kind: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps({"kind": kind, "payload": payload}, sort_keys=True, default=str).encode()
    ).hexdigest()


def submit_job(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    kind: str,
    payload: dict[str, Any],
    idempotency_key: str | None = None,
    request_id: str | None = None,
    timeout_seconds: int | None = None,
    dispatch: bool = True,
) -> tuple[Job, bool]:
    """Create (or return the existing idempotent) job. Commits the session."""
    s = get_settings()
    if idempotency_key:
        existing = db.scalar(
            select(Job).where(
                Job.workspace_id == workspace_id, Job.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            if payload_hash(existing.kind, existing.payload) != payload_hash(kind, payload):
                raise ApiError(
                    409,
                    "idempotency_conflict",
                    "Idempotency-Key was already used with a different request",
                )
            return existing, False
    job = Job(
        workspace_id=workspace_id,
        kind=kind,
        payload=payload,
        idempotency_key=idempotency_key,
        status=JobStatus.queued,
        max_attempts=s.job_max_attempts,
        timeout_seconds=timeout_seconds or s.job_default_timeout_seconds,
        request_id=request_id,
        created_by=user_id,
    )
    db.add(job)
    db.commit()
    if dispatch:
        enqueue(job)
    return job, True


def request_cancel(db: Session, job: Job) -> Job:
    if job.status in TERMINAL:
        return job
    if job.status == JobStatus.queued:
        res = db.execute(
            update(Job)
            .where(Job.id == job.id, Job.status == JobStatus.queued)
            .values(
                status=JobStatus.cancelled,
                cancel_requested=True,
                finished_at=now(),
                message="cancelled before start",
            )
        )
        if res.rowcount:  # type: ignore[attr-defined]
            _mirror_status(db, job, JobStatus.cancelled)
            db.commit()
            db.refresh(job)
            return job
    db.execute(update(Job).where(Job.id == job.id).values(cancel_requested=True))
    db.commit()
    db.refresh(job)
    return job


def _mirror_status(db: Session, job: Job | Any, status: JobStatus) -> None:
    payload = job.payload or {}
    if "experiment_id" in payload and job.kind == "backtest":
        db.execute(
            update(Experiment)
            .where(Experiment.id == uuid.UUID(payload["experiment_id"]))
            .values(status=status)
        )
    if "research_run_id" in payload:
        db.execute(
            update(ResearchRun)
            .where(ResearchRun.id == uuid.UUID(payload["research_run_id"]))
            .values(status=status)
        )


# --------------------------------------------------------------------------- execution
@dataclass
class JobContext:
    job_id: uuid.UUID
    kind: str
    attempt: int
    worker_id: str
    timeout_seconds: int
    payload: dict[str, Any]
    started: float = field(default_factory=time.monotonic)
    _last_beat: float = 0.0
    _cancel: bool = False

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def heartbeat(
        self, progress: float | None = None, message: str | None = None, force: bool = False
    ) -> None:
        """Extend the lease, record progress, and observe cancellation/timeouts."""
        if self.elapsed() > self.timeout_seconds:
            raise JobTimeoutError(f"exceeded {self.timeout_seconds}s")
        t = time.monotonic()
        if not force and t - self._last_beat < 0.5:
            if self._cancel:
                raise JobCancelledError("cancel requested")
            return
        self._last_beat = t
        lease = get_settings().job_lease_seconds
        values: dict[str, Any] = {
            "heartbeat_at": now(),
            "lease_expires_at": now() + timedelta(seconds=lease),
        }
        if progress is not None:
            values["progress"] = max(0.0, min(1.0, float(progress)))
        if message is not None:
            values["message"] = message[:500]
        with SessionLocal() as db:
            row = db.execute(
                update(Job)
                .where(
                    Job.id == self.job_id,
                    Job.attempts == self.attempt,
                    Job.status == JobStatus.running,
                )
                .values(**values)
                .returning(Job.cancel_requested)
            ).first()
            db.commit()
        if row is None:
            raise LeaseLostError("job lease was taken over or job is no longer running")
        if row[0]:
            self._cancel = True
            raise JobCancelledError("cancel requested")

    def should_cancel(self) -> bool:
        try:
            self.heartbeat()
        except (JobCancelledError, JobTimeoutError):
            return True
        return False


Finalizer = Callable[[Session], dict[str, Any] | None]
Handler = Callable[[JobContext], Finalizer]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return deco


def claim(job_id: uuid.UUID, worker_id: str) -> Job | None:
    lease = get_settings().job_lease_seconds
    t = now()
    with SessionLocal() as db:
        row = db.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.cancel_requested.is_(False),
                or_(
                    Job.status == JobStatus.queued,
                    (Job.status == JobStatus.running) & (Job.lease_expires_at < t),
                ),
                Job.attempts < Job.max_attempts,
            )
            .values(
                status=JobStatus.running,
                attempts=Job.attempts + 1,
                worker_id=worker_id,
                lease_expires_at=t + timedelta(seconds=lease),
                heartbeat_at=t,
                started_at=t,
                error=None,
                message="running",
            )
            .returning(Job)
        ).scalar_one_or_none()
        if row is not None:
            _mirror_status(db, row, JobStatus.running)
        db.commit()
        return row


def _finish(
    job_id: uuid.UUID,
    attempt: int,
    status: JobStatus,
    *,
    message: str = "",
    error: dict[str, Any] | None = None,
    progress: float | None = None,
) -> bool:
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None or job.attempts != attempt or job.status != JobStatus.running:
            db.rollback()
            return False
        job.status = status
        job.message = message[:500]
        job.error = error
        job.finished_at = now()
        job.lease_expires_at = None
        if progress is not None:
            job.progress = progress
        _mirror_status(db, job, status)
        db.commit()
        return True


def execute_job(job_id: str, worker_id: str | None = None) -> str:
    """Entry point invoked by RQ (or directly in tests). Returns the outcome label."""
    jid = uuid.UUID(job_id)
    wid = worker_id or worker_identity()
    job = claim(jid, wid)
    if job is None:
        log.info("job_skip", job_id=job_id, reason="not claimable")
        return "skipped"
    if job.attempts == 1 and job.created_at is not None:
        JOB_QUEUE_WAIT_SECONDS.labels(job.kind).observe(
            max(0.0, (now() - job.created_at).total_seconds())
        )
    ctx = JobContext(
        job_id=jid,
        kind=job.kind,
        attempt=job.attempts,
        worker_id=wid,
        timeout_seconds=job.timeout_seconds,
        payload=dict(job.payload),
    )
    bound = log.bind(job_id=job_id, kind=job.kind, attempt=job.attempts, request_id=job.request_id)
    bound.info("job_start")
    JOBS_RUNNING.inc()
    t0 = time.monotonic()
    outcome = "failed"
    try:
        fn = HANDLERS.get(job.kind)
        if fn is None:
            raise PermanentJobError(f"no handler for job kind {job.kind!r}")
        finalize = fn(ctx)
        ctx.heartbeat(progress=1.0, message="committing", force=True)
        with SessionLocal() as db:
            locked = db.scalar(select(Job).where(Job.id == jid).with_for_update())
            if (
                locked is None
                or locked.attempts != ctx.attempt
                or locked.status != JobStatus.running
            ):
                db.rollback()
                raise LeaseLostError("lost lease before commit")
            summary = finalize(db)
            locked.status = JobStatus.completed
            locked.progress = 1.0
            locked.message = (summary or {}).get("message", "completed")[:500]
            locked.finished_at = now()
            locked.lease_expires_at = None
            _mirror_status(db, locked, JobStatus.completed)
            db.commit()
        outcome = "completed"
    except JobCancelledError:
        outcome = "cancelled"
        _finish(jid, ctx.attempt, JobStatus.cancelled, message="cancelled by user")
    except JobTimeoutError as exc:
        outcome = "timed_out"
        _finish(
            jid,
            ctx.attempt,
            JobStatus.timed_out,
            message=str(exc),
            error={"type": "JobTimeoutError", "message": str(exc)},
        )
    except LeaseLostError as exc:
        outcome = "lease_lost"
        bound.warning("job_lease_lost", error=str(exc))
    except Exception as exc:
        permanent = isinstance(exc, PermanentJobError | ValueError | KeyError) or type(
            exc
        ).__name__ in {"BacktestError", "ValidationError", "OptimizationError"}
        err = {
            "type": type(exc).__name__,
            "message": str(exc)[:2000],
            "retryable": not permanent,
            "trace": traceback.format_exc(limit=8)[-4000:],
        }
        if not permanent and ctx.attempt < job.max_attempts:
            outcome = "retry"
            delay = min(60.0, 2.0**ctx.attempt)
            with SessionLocal() as db:
                res = db.execute(
                    update(Job)
                    .where(
                        Job.id == jid, Job.attempts == ctx.attempt, Job.status == JobStatus.running
                    )
                    .values(
                        status=JobStatus.queued,
                        error=err,
                        lease_expires_at=None,
                        message=f"retrying in {delay:.0f}s after {type(exc).__name__}",
                    )
                )
                db.commit()
                again = db.get(Job, jid)
            if res.rowcount and again is not None:  # type: ignore[attr-defined]
                JOB_RETRIES.labels(job.kind, type(exc).__name__).inc()
                enqueue(again, delay_seconds=delay)
        else:
            outcome = "failed"
            _finish(jid, ctx.attempt, JobStatus.failed, message=str(exc)[:500], error=err)
        bound.error("job_error", error=str(exc), outcome=outcome)
    finally:
        JOBS_RUNNING.dec()
        JOB_RUN_SECONDS.labels(job.kind, outcome).observe(time.monotonic() - t0)
        JOB_OUTCOMES.labels(job.kind, outcome).inc()
        bound.info("job_end", outcome=outcome, seconds=round(time.monotonic() - t0, 3))
    return outcome


# --------------------------------------------------------------------------- recovery
def reap(redeliver_after_seconds: int = 120) -> dict[str, int]:
    """Recover jobs whose worker died and re-deliver queued jobs that were never picked up."""
    t = now()
    stats = {"requeued": 0, "failed": 0, "timed_out": 0, "redelivered": 0}
    with SessionLocal() as db:
        stale = db.scalars(
            select(Job)
            .where(Job.status == JobStatus.running, Job.lease_expires_at < t)
            .with_for_update(skip_locked=True)
        ).all()
        for job in stale:
            started = job.started_at or job.created_at
            if (t - started).total_seconds() > job.timeout_seconds:
                job.status = JobStatus.timed_out
                job.message = "exceeded timeout (worker unresponsive)"
                job.finished_at = t
                stats["timed_out"] += 1
            elif job.attempts >= job.max_attempts:
                job.status = JobStatus.failed
                job.error = {
                    "type": "LeaseExpired",
                    "message": "worker stopped heartbeating",
                    "retryable": False,
                }
                job.message = "failed: worker lost and attempts exhausted"
                job.finished_at = t
                stats["failed"] += 1
            else:
                job.status = JobStatus.queued
                job.message = "requeued after worker loss"
                job.lease_expires_at = None
                stats["requeued"] += 1
                JOB_RETRIES.labels(job.kind, "lease_expired").inc()
            _mirror_status(db, job, job.status)
        db.commit()
        for job in stale:
            if job.status == JobStatus.queued:
                enqueue(job)
        cutoff = t - timedelta(seconds=redeliver_after_seconds)
        waiting = db.scalars(
            select(Job)
            .where(
                Job.status == JobStatus.queued,
                Job.created_at < cutoff,
                or_(Job.heartbeat_at.is_(None), Job.heartbeat_at < cutoff),
            )
            .limit(500)
        ).all()
        for job in waiting:
            if enqueue(job):
                stats["redelivered"] += 1
    if any(stats.values()):
        log.info("reaper_cycle", **stats)
    return stats
