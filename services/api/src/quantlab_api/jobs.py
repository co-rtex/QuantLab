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
import secrets
import socket
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from redis import Redis
from rq import Queue, get_current_job
from rq.job import Job as RQJob
from rq.serializers import JSONSerializer
from sqlalchemy import create_engine, func, or_, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from quantlab_api.config import get_settings
from quantlab_api.db import SessionLocal, get_engine
from quantlab_api.errors import ApiError
from quantlab_api.models import TERMINAL, Experiment, Job, JobStatus, ResearchRun, User, Workspace
from quantlab_api.observability import (
    JOB_OUTCOMES,
    JOB_QUEUE_WAIT_SECONDS,
    JOB_RETRIES,
    JOB_RUN_SECONDS,
    JOBS_RUNNING,
    log,
)
from quantlab_api.tracing import attach_carrier, current_carrier, detach, flush, tracer

# Exceptions whose messages are written by QuantLab for users (never raw driver errors).
SAFE_ERROR_TYPES = frozenset(
    {
        "PermanentJobError",
        "BacktestError",
        "ValidationError",
        "ValueError",
        "OptimizationError",
        "InfeasibleConstraints",
        "JobTimeoutError",
        "LeaseExpired",
        "LedgerError",
        "ResourceExhausted",
    }
)


class JobCancelledError(Exception):
    pass


class JobTimeoutError(Exception):
    pass


class PermanentJobError(Exception):
    """Failure that retrying cannot fix (invalid input, missing data)."""


class LeaseLostError(Exception):
    pass


def db_now() -> Any:
    """The database's clock, for every lease, backoff and claim (#130).

    Hosts' clocks differ: a deadline one worker wrote with its clock and another compared
    with its own let a delayed retry run early, or a lease look expired early (a second
    worker then ran the job too, until fencing stopped its commit). ``now()`` is the
    statement's transaction time in PostgreSQL, the same for every row it touches.
    """
    return func.now()


def now() -> datetime:
    return datetime.now(UTC)


_identity: str | None = None


def worker_identity() -> str:
    """This process's worker name: ``QUANTLAB_WORKER_NAME``, or host, pid and a random
    suffix chosen once per process start (#162).

    Host and pid alone repeat across restarts: a container keeps its hostname and runs the
    worker as pid 1, so the new process asked RQ for the name its dead predecessor still
    held, and exited until that registration expired (``worker_ttl`` + 60 s, 8 minutes).
    The name is fixed for the life of the process and a forked work horse inherits it, so
    a job's ``worker_id`` is the RQ worker's name. It never contains ':' (RQ's key
    separator) and fits ``jobs.worker_id``.
    """
    global _identity
    if _identity is None:
        override = os.environ.get("QUANTLAB_WORKER_NAME")
        if override:
            if ":" in override or len(override) > 128:
                raise ValueError("QUANTLAB_WORKER_NAME must be at most 128 characters, without ':'")
            _identity = override
        else:
            # At most 100 + 1 + 7 + 1 + 8 characters, however long the hostname.
            host = socket.gethostname().replace(":", "-")[:100]
            _identity = f"{host}-{os.getpid()}-{secrets.token_hex(4)}"
    return _identity


# --------------------------------------------------------------------------- queueing
_redis: Redis | None = None  # type: ignore[type-arg]

#: The one callable a worker runs from the queue. Everything a job needs is in its
#: PostgreSQL row (ADR-0002), so the queue entry is only a pointer to it (#115).
TRUSTED_FUNC = "quantlab_api.jobs.execute_job"


class UntrustedJobError(RuntimeError):
    """A queue entry that is not ``execute_job(<job id>)``: never run."""


class TrustedJob(RQJob):
    """An RQ job that runs nothing but :data:`TRUSTED_FUNC` with one job id (#115).

    RQ's default serializer is pickle, so anyone who could write to Redis could make a
    worker run arbitrary code while it unpickled a job; the JSON serializer closes that.
    It does not close the rest: RQ stores the callable *by name* and imports it on the
    worker, and so it does the success, failure and stopped callbacks, and it delivers
    any webhooks the entry lists. A JSON entry naming ``os.system``, or failing on purpose
    with a failure callback, would still run. This class checks the callable and its
    arguments before RQ resolves anything, never resolves a callback, and never delivers
    a webhook: the application uses neither.
    """

    def restore(self, raw_data: Any) -> Any:
        result = super().restore(raw_data)
        self.webhooks = []  # never registered by QuantLab: an entry's list is not trusted
        return result

    @property
    def func(self) -> Any:
        self._require_trusted()
        return super().func

    # Callbacks are imported by name, like the callable: none is ever run.
    @property
    def success_callback(self) -> None:
        return None

    @property
    def failure_callback(self) -> None:
        return None

    @property
    def stopped_callback(self) -> None:
        return None

    def _require_trusted(self) -> None:
        args, kwargs = self.args, self.kwargs
        if (
            self.func_name != TRUSTED_FUNC
            or self.instance is not None
            or kwargs
            or len(args) != 1
            or not isinstance(args[0], str)
            or not _is_uuid(args[0])
        ):
            log.error(
                "untrusted_job_refused", rq_job_id=self.id, func_name=str(self.func_name)[:200]
            )
            raise UntrustedJobError(f"refusing to run queue entry {self.id!r}")


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


#: How every queue and worker must talk to Redis: JSON, never pickle, and only trusted
#: jobs. A worker built without these would unpickle whatever is on the queue.
RQ_OPTIONS: dict[str, Any] = {"serializer": JSONSerializer, "job_class": TrustedJob}


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
    return Queue(get_settings().queue_name, connection=redis_conn(), **RQ_OPTIONS)


def enqueue(job: Job, delay_seconds: float = 0.0, delivery_id: str | None = None) -> bool:
    """Best effort: if Redis is down the job stays queued and the reaper re-delivers it.

    ``delivery_id`` names the queue entry, so enqueueing the same delivery again replaces
    it rather than adding another."""
    try:
        q = get_queue()
        kwargs: dict[str, Any] = {
            "job_timeout": job.timeout_seconds + 60,
            "result_ttl": 3600,
            "failure_ttl": 86400,
            "description": f"{job.kind}:{job.id}",
        }
        if delivery_id is not None:
            kwargs["job_id"] = delivery_id
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


def lock_idempotency_key(db: Session, workspace_id: uuid.UUID, key: str | None) -> None:
    """Serialize the requests that share an Idempotency-Key, until this transaction ends.

    Routes that create a run row before ``submit_job`` (experiments, ML runs, validation)
    call this before they look the key up. Otherwise two concurrent retries both found no
    job for the key, both inserted a run, and the second hashed a payload naming its own
    run: 409 ``idempotency_conflict`` for a genuine retry, plus a phantom row. Taken
    before ``submit_job``'s workspace and user locks, always in that order. Requests
    without a key are not serialized.
    """
    if key:
        # A waiter holds a pooled connection, so it gives up after a while (#43).
        advisory_lock(db, f"idempotency:{workspace_id}:{key}", timeout=IDEMPOTENCY_WAIT)


# How long a request waits for an earlier request with the same Idempotency-Key.
IDEMPOTENCY_WAIT = "15s"
LOCK_NOT_AVAILABLE = "55P03"  # PostgreSQL SQLSTATE raised when lock_timeout expires


def advisory_lock(db: Session, name: str, *, timeout: str | None = None) -> None:
    """A PostgreSQL advisory lock named ``name``, held until the transaction ends.

    Callers that take several take them in a fixed order (an idempotency key first). With
    ``timeout``, a request that cannot get the lock in time gets 409 and may retry.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    if timeout is None:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": name})
        return
    # set_config(..., true) is SET LOCAL, but takes a bind parameter.
    db.execute(text("SELECT set_config('lock_timeout', :t, true)"), {"t": timeout})
    try:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": name})
    except OperationalError as exc:
        # Only lock_timeout (55P03, lock_not_available) means "someone holds the key".
        # A cancelled statement, a shutdown or a dropped connection is a real error.
        if getattr(exc.orig, "sqlstate", None) != LOCK_NOT_AVAILABLE:
            raise
        try:
            db.rollback()  # ends the aborted transaction, and with it the SET LOCAL
        except Exception as rollback_exc:
            # The 409 below is still right; the session is discarded with the request.
            log.warning("rollback_after_lock_timeout_failed", error=type(rollback_exc).__name__)
        raise ApiError(
            409, "request_in_progress", "An identical request is still in progress; retry"
        ) from exc
    db.execute(text("SET LOCAL lock_timeout = DEFAULT"))


def request_fingerprint(endpoint: str, user_id: uuid.UUID, body: Any) -> str:
    """What the client asked for: the endpoint (with its path ids), the user and the
    request body. An Idempotency-Key replays only a request with the same fingerprint."""
    doc = {"endpoint": endpoint, "user": str(user_id), "body": body}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()


def replayed_job(
    db: Session,
    workspace_id: uuid.UUID,
    key: str | None,
    fingerprint: str,
    legacy_match: Callable[[Job], bool],
) -> Job | None:
    """The job an identical earlier request created with this key, or None for a new key.

    Keys are scoped to the workspace: a key another member already used, or one used for a
    different endpoint, parent, body or name, is 409 ``idempotency_conflict`` (#43). Jobs
    created before fingerprints were stored are compared with ``legacy_match``. Call after
    ``lock_idempotency_key``. The fingerprint goes into the new job's payload as
    ``request_fingerprint``.
    """
    if not key:
        return None
    prior = db.scalar(
        select(Job).where(Job.workspace_id == workspace_id, Job.idempotency_key == key)
    )
    if prior is None:
        return None
    stored = (prior.payload or {}).get("request_fingerprint")
    if not (stored == fingerprint if stored is not None else legacy_match(prior)):
        raise ApiError(
            409, "idempotency_conflict", "Idempotency-Key was already used with a different request"
        )
    return prior


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
    on_created: Callable[[Job], None] | None = None,
) -> tuple[Job, bool]:
    """Create (or return the existing idempotent) job. Commits the session.

    ``on_created`` runs after the new job has its id and before that commit, so a caller
    can link its run row to the job atomically. A retry that finds the job by its
    Idempotency-Key then always finds the linked run too.
    """
    s = get_settings()

    def replay() -> Job | None:
        if not idempotency_key:
            return None
        existing = db.scalar(
            select(Job).where(
                Job.workspace_id == workspace_id, Job.idempotency_key == idempotency_key
            )
        )
        if existing is not None and payload_hash(existing.kind, existing.payload) != (
            payload_hash(kind, payload)
        ):
            raise ApiError(
                409,
                "idempotency_conflict",
                "Idempotency-Key was already used with a different request",
            )
        return existing

    if (existing := replay()) is not None:
        return existing, False
    # Lock the workspace row so concurrent submissions cannot all pass the cap check.
    # NO KEY UPDATE, not UPDATE: callers have usually inserted rows referencing the
    # workspace already (audit event, experiment), and each such foreign-key check holds
    # KEY SHARE on it. FOR UPDATE conflicts with KEY SHARE, so two concurrent submissions
    # deadlocked and one returned 500; NO KEY UPDATE still serializes submissions.
    db.execute(
        select(Workspace.id).where(Workspace.id == workspace_id).with_for_update(key_share=True)
    )
    # Serialize this account's submissions across *all* workspaces. Locking the
    # user row with NO KEY UPDATE permits FK KEY SHARE references from the request
    # transaction (e.g. a newly created dataset), avoiding the former deadlock.
    db.execute(select(User.id).where(User.id == user_id).with_for_update(key_share=True))
    # Recheck idempotency after both locks: a concurrent replay should never be
    # blocked by a quota consumed by its own original job.
    if (existing := replay()) is not None:
        return existing, False
    user_active = (
        db.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.created_by == user_id,
                Job.status.in_([JobStatus.queued, JobStatus.running]),
            )
        )
        or 0
    )
    # A lower workspace cap also bounds the account cap. Otherwise configuring
    # two jobs/workspace would still allow five workspaces to run ten jobs.
    account_cap = min(s.max_active_jobs_per_user, s.max_active_jobs_per_workspace)
    if user_active >= account_cap:
        raise ApiError(
            429,
            "too_many_jobs",
            "Your account has reached its limit of queued or running jobs; wait for some to finish",
        )
    active = (
        db.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.workspace_id == workspace_id,
                Job.status.in_([JobStatus.queued, JobStatus.running]),
            )
        )
        or 0
    )
    if active >= s.max_active_jobs_per_workspace:
        raise ApiError(
            429,
            "too_many_jobs",
            f"This workspace already has {active} queued or running jobs; wait for some to finish",
        )
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
        trace_context=current_carrier() or None,
    )
    db.add(job)
    if on_created is not None:
        db.flush()
        on_created(job)
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
                finished_at=db_now(),
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
    if "ml_run_id" in payload:
        from quantlab_api.models import MLRun

        db.execute(
            update(MLRun).where(MLRun.id == uuid.UUID(payload["ml_run_id"])).values(status=status)
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
            "heartbeat_at": db_now(),
            "lease_expires_at": db_now() + timedelta(seconds=lease),
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
    t = db_now()  # the statement's own clock: one host's skew cannot move a deadline
    with SessionLocal() as db:
        row = db.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.cancel_requested.is_(False),
                or_(
                    # A queued retry only once its backoff has passed (#123): an early or
                    # forged delivery claims nothing, and so consumes no attempt.
                    (Job.status == JobStatus.queued)
                    & or_(Job.not_before.is_(None), Job.not_before <= t),
                    (Job.status == JobStatus.running) & (Job.lease_expires_at < t),
                ),
                Job.attempts < Job.max_attempts,
            )
            .values(
                status=JobStatus.running,
                not_before=None,
                attempts=Job.attempts + 1,
                worker_id=worker_id,
                lease_expires_at=t + timedelta(seconds=lease),
                heartbeat_at=t,
                started_at=func.coalesce(Job.started_at, t),
                attempt_started_at=t,  # the timeout is per attempt (#167)
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
        job.finished_at = db_now()
        job.lease_expires_at = None
        if progress is not None:
            job.progress = progress
        _mirror_status(db, job, status)
        db.commit()
        return True


def _redeliver_when_due(job_id: uuid.UUID) -> None:
    """A delivery that arrived before a queued retry's backoff ends (early, or forged) is
    put back for when the job is due, so a scheduler running slightly fast cannot strand a
    retry until the reaper's next pass (#123)."""
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None or job.status != JobStatus.queued or job.cancel_requested:
            return
        t = db.scalar(select(db_now()))
        if job.not_before is not None and t is not None and job.not_before > t:
            # One delivery per due time: k early (or forged) deliveries reschedule it once.
            due = job.not_before
            # No ":" in it: RQ registries key entries as "<job id>:<execution id>".
            delivery = f"{job.id}-{round(due.timestamp() * 1_000_000)}"
            running = get_current_job()
            if running is not None and running.id == delivery:
                # This entry is that delivery, arriving early. Under its own name the
                # reschedule shares the hash of the entry being run: RQ 3.0 still delivers
                # it when due, but the worker marks it finished in the meantime, so the
                # queue shows a pending delivery as done. Another name keeps them apart.
                delivery += "-again"
            enqueue(job, delay_seconds=(due - t).total_seconds(), delivery_id=delivery)


def execute_job(job_id: str, worker_id: str | None = None) -> str:
    """Entry point invoked by RQ (or directly in tests). Returns the outcome label."""
    jid = uuid.UUID(job_id)
    wid = worker_id or worker_identity()
    job = claim(jid, wid)
    if job is None:
        _redeliver_when_due(jid)
        log.info("job_skip", job_id=job_id, reason="not claimable")
        return "skipped"
    token = attach_carrier(job.trace_context)
    try:
        with tracer.start_as_current_span(
            "job.execute",
            attributes={
                "quantlab.job.id": job_id,
                "quantlab.job.kind": job.kind,
                "quantlab.job.attempt": job.attempts,
            },
        ) as span:
            outcome = _run_claimed(job, jid, wid)
            span.set_attribute("quantlab.job.outcome", outcome)
            return outcome
    finally:
        detach(token)
        flush()


def _run_claimed(job: Job, jid: uuid.UUID, wid: str) -> str:
    job_id = str(jid)
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
        with tracer.start_as_current_span("job.handler"), _Keepalive(jid, ctx.attempt):
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
            locked.finished_at = db_now()
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
        cancelled = _cancel_requested(jid)
        if cancelled:
            outcome = "cancelled"
            _finish(jid, ctx.attempt, JobStatus.cancelled, message="cancelled by user")
        elif not permanent and ctx.attempt < job.max_attempts:
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
                        not_before=db_now() + timedelta(seconds=delay),
                        message=f"retrying in {delay:.0f}s after {type(exc).__name__}",
                    )
                )
                if res.rowcount:  # type: ignore[attr-defined]
                    _mirror_status(db, job, JobStatus.queued)
                db.commit()
                again = db.get(Job, jid)
            if res.rowcount and again is not None:  # type: ignore[attr-defined]
                JOB_RETRIES.labels(job.kind, type(exc).__name__).inc()
                enqueue(again, delay_seconds=delay)
        else:
            outcome = "failed"
            public = (
                str(exc)[:500]
                if type(exc).__name__ in SAFE_ERROR_TYPES
                else f"failed: {type(exc).__name__} (details in server logs)"
            )
            _finish(jid, ctx.attempt, JobStatus.failed, message=public, error=err)
        bound.error("job_error", error=str(exc), outcome=outcome)
    finally:
        JOBS_RUNNING.dec()
        JOB_RUN_SECONDS.labels(job.kind, outcome).observe(time.monotonic() - t0)
        JOB_OUTCOMES.labels(job.kind, outcome).inc()
        bound.info("job_end", outcome=outcome, seconds=round(time.monotonic() - t0, 3))
    return outcome


def _cancel_requested(jid: uuid.UUID) -> bool:
    with SessionLocal() as db:
        return bool(db.scalar(select(Job.cancel_requested).where(Job.id == jid)))


class _Keepalive:
    """Extends the lease from a background thread while a handler runs, so long phases
    without progress callbacks (analytics, artifact writes, model fits) cannot lose the
    lease to another worker. Fencing still applies: it only touches the current attempt."""

    def __init__(self, jid: uuid.UUID, attempt: int) -> None:
        import threading

        self.jid, self.attempt = jid, attempt
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"keepalive-{jid}")

    def _run(self) -> None:
        lease = get_settings().job_lease_seconds
        while not self.stop.wait(max(1.0, lease / 3)):
            try:
                with SessionLocal() as db:
                    db.execute(
                        update(Job)
                        .where(
                            Job.id == self.jid,
                            Job.attempts == self.attempt,
                            Job.status == JobStatus.running,
                        )
                        .values(
                            lease_expires_at=db_now() + timedelta(seconds=lease),
                            heartbeat_at=db_now(),
                        )
                    )
                    db.commit()
            except Exception as exc:  # transient DB errors: the next tick retries
                log.warning("keepalive_failed", job_id=str(self.jid), error=str(exc))

    def __enter__(self) -> _Keepalive:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()
        self.thread.join(timeout=5)


# --------------------------------------------------------------------------- recovery
# ------------------------------------------------------------- out-of-memory kills (#163)
CGROUP = "/sys/fs/cgroup"  # cgroup v2, as in the worker containers


def _cgroup_read(name: str) -> str | None:
    try:
        with open(os.path.join(CGROUP, name)) as f:
            return f.read()
    except OSError:  # not on Linux, or no cgroup v2: no evidence either way
        return None


def _oom_kills() -> int | None:
    """The container's count of processes the kernel killed for exceeding memory."""
    text = _cgroup_read("memory.events")
    for line in (text or "").splitlines():
        key, _, value = line.partition(" ")
        if key == "oom_kill" and value.strip().isdigit():
            return int(value)
    return None


def _memory_limit_mb() -> float | None:
    text = (_cgroup_read("memory.max") or "").strip()
    return int(text) / 2**20 if text.isdigit() else None


_oom_kills_seen = _oom_kills()  # at worker start; each kill handled moves it on


def _drop_inherited_connections() -> None:
    """In a forked child, forget the parent's pooled connections without closing them.

    A work horse is a fork of the RQ worker. A connection both processes use corrupts
    both: psycopg's prepared-statement names restart in the child, and a transaction the
    child left open could be committed by the parent (#174 review). The child opens its
    own connections instead.
    """
    get_engine().dispose(close=False)


os.register_at_fork(after_in_child=_drop_inherited_connections)

_oom_engines: dict[str, Engine] = {}


def _oom_engine() -> Engine:
    """The handler's own engine, without a pool: it runs in the RQ worker, whose open
    connections every later horse would inherit, so it keeps none open afterwards."""
    url = get_settings().database_url
    if url not in _oom_engines:
        _oom_engines[url] = create_engine(url, poolclass=NullPool)
    return _oom_engines[url]


def horse_killed(rq_job: RQJob, retpid: int, ret_val: int, rusage: Any) -> None:
    """RQ's ``work_horse_killed_handler``: runs in the worker when its horse dies.

    A horse the kernel killed for exceeding the container's memory would otherwise surface
    minutes later as ``LeaseExpired`` ("worker stopped heartbeating"), and be retried into
    the same wall. An out-of-memory kill is recorded at once as ``ResourceExhausted``, not
    retryable, because the same job needs the same memory again. The evidence is a SIGKILL
    together with a rise in the cgroup's ``oom_kill`` count, or a peak resident set at the
    memory limit. Any other death is left to the reaper as before (``LeaseExpired``).

    RQ calls it bare, so an exception here would stop the worker: any failure is logged
    and the job is left to the reaper.
    """
    try:
        _classify_horse_death(rq_job, retpid, ret_val, rusage)
    except Exception as exc:
        log.error("horse_killed_failed", error=repr(exc)[:300])


def _classify_horse_death(rq_job: RQJob, retpid: int, ret_val: int, rusage: Any) -> None:
    global _oom_kills_seen
    signalled = ret_val is not None and os.WIFSIGNALED(ret_val)
    if not signalled or os.WTERMSIG(ret_val) != 9:
        return
    kills, limit = _oom_kills(), _memory_limit_mb()
    rose = kills is not None and _oom_kills_seen is not None and kills > _oom_kills_seen
    _oom_kills_seen = kills
    peak = getattr(rusage, "ru_maxrss", 0) / 2**10  # kilobytes on Linux
    at_limit = limit is not None and peak >= 0.95 * limit
    if not (rose or at_limit):
        return
    args = getattr(rq_job, "args", None) or []
    if len(args) != 1 or not isinstance(args[0], str) or not _is_uuid(args[0]):
        return
    # The attempt that died: since #170 the horse inherits the worker's fixed name, so the
    # worker's own identity matches; host:<its pid> is what a horse claimed as before. A
    # job another worker has reclaimed is left alone.
    horse = {worker_identity(), f"{socket.gethostname()}:{retpid}"}
    limit_text = f" of the worker's {limit:.0f} MB limit" if limit else ""
    with Session(_oom_engine()) as db:
        job = db.get(Job, uuid.UUID(args[0]), with_for_update=True)
        if job is None or job.status != JobStatus.running or job.worker_id not in horse:
            return
        job.status = JobStatus.failed
        job.error = {
            "type": "ResourceExhausted",
            "message": (
                f"out of memory: the job was stopped after using {peak:.0f} MB{limit_text}. "
                "A smaller universe, a shorter period or a less frequent rebalance needs "
                "less memory."
            ),
            "retryable": False,
        }
        job.message = "failed: out of memory"
        job.lease_expires_at = None
        job.finished_at = db_now()
        kind = job.kind
        _mirror_status(db, job, JobStatus.failed)
        db.commit()
    JOB_OUTCOMES.labels(kind, "failed").inc()
    log.warning("job_out_of_memory", job_id=args[0], peak_mb=round(peak), limit_mb=limit)


def _count(decided: list[tuple[str, str]], requeued: list[str]) -> None:
    """The reaper's decisions in the job metrics, as the worker counts its own (#179)."""
    for kind, outcome in decided:
        JOB_OUTCOMES.labels(kind, outcome).inc()
    for kind in requeued:
        JOB_RETRIES.labels(kind, "lease_expired").inc()


def reap(redeliver_after_seconds: int = 120) -> dict[str, int]:
    """Recover jobs whose worker died and re-deliver queued jobs that were never picked up."""
    stats = {"requeued": 0, "failed": 0, "timed_out": 0, "redelivered": 0, "uploads_swept": 0}
    with SessionLocal() as db:
        # The database's time, not this host's (#130): leases and backoffs were written in
        # it, and a reaper with a fast clock would otherwise requeue live jobs.
        t = db.scalar(select(db_now()))
        if t is None:  # not an assert: those are stripped under -O
            raise RuntimeError("the database returned no time")
        stale = db.scalars(
            select(Job)
            .where(Job.status == JobStatus.running, Job.lease_expires_at < t)
            .with_for_update(skip_locked=True)
        ).all()
        decided: list[tuple[str, str]] = []  # (kind, outcome) of jobs it finished
        requeued: list[str] = []  # kinds it requeued after a lost worker
        for job in stale:
            # How long this attempt ran: from its claim until its lease ran out (#167).
            # Waiting for a worker between attempts does not count, and neither does the
            # time since the lease expired: a reaper that was down through an outage would
            # otherwise time out every job that was running when it began. The lease's end,
            # not the last heartbeat: keepalive beats every lease/3, so with a long lease
            # an attempt RQ killed for overrunning (at timeout + 60 s) could have last beaten
            # before the timeout. This over-counts by at most one lease. Rows from before
            # attempt_started_at existed are timed from their first start.
            started = job.attempt_started_at or job.started_at or job.created_at
            ran = min(t, job.lease_expires_at or t) - started
            if job.cancel_requested:
                job.status = JobStatus.cancelled
                job.message = "cancelled (worker stopped before observing the request)"
                job.finished_at = t
                stats["cancelled"] = stats.get("cancelled", 0) + 1
                decided.append((job.kind, JobStatus.cancelled.value))
            elif ran.total_seconds() > job.timeout_seconds:
                job.status = JobStatus.timed_out
                job.message = "exceeded timeout (worker unresponsive)"
                job.finished_at = t
                stats["timed_out"] += 1
                decided.append((job.kind, JobStatus.timed_out.value))
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
                decided.append((job.kind, JobStatus.failed.value))
            else:
                job.status = JobStatus.queued
                job.message = "requeued after worker loss"
                job.lease_expires_at = None
                stats["requeued"] += 1
                requeued.append(job.kind)
            _mirror_status(db, job, job.status)
        db.commit()
        # Counted once committed (#179): a failed commit decided nothing, and the next pass
        # decides again, so counting first would count it twice.
        _count(decided, requeued)
        for job in stale:
            if job.status == JobStatus.queued:
                enqueue(job)
        cutoff = t - timedelta(seconds=redeliver_after_seconds)
        orphaned = db.scalars(
            select(Job)
            .where(Job.status == JobStatus.queued, Job.cancel_requested.is_(True))
            .with_for_update(skip_locked=True)
        ).all()
        for job in orphaned:  # cancel requested but never observed by a worker
            job.status = JobStatus.cancelled
            job.message = "cancelled"
            job.finished_at = t
            _mirror_status(db, job, JobStatus.cancelled)
        if orphaned:
            stats["cancelled"] = stats.get("cancelled", 0) + len(orphaned)
            db.commit()
            _count([(job.kind, JobStatus.cancelled.value) for job in orphaned], [])
        waiting = db.scalars(
            select(Job)
            .where(
                Job.status == JobStatus.queued,
                Job.cancel_requested.is_(False),
                Job.created_at < cutoff,
                or_(Job.heartbeat_at.is_(None), Job.heartbeat_at < cutoff),
                or_(Job.not_before.is_(None), Job.not_before <= t),  # its backoff is over
            )
            .order_by(Job.created_at)
            .limit(500)
        ).all()
        for job in waiting:
            if enqueue(job):
                stats["redelivered"] += 1
                # Record the delivery so the next cycle leaves it alone until the cutoff
                # passes again; without this every 15 s cycle re-enqueued the whole backlog.
                job.heartbeat_at = t
        db.commit()
    stats["uploads_swept"] = sweep_uploads()
    from quantlab_api.storage import purge_trash_if_due, sweep_orphans  # no load cycle

    swept = sweep_orphans()  # throttled inside: at most once per storage_sweep_interval
    stats["orphans_swept"] = sum(
        int(swept.get(k, 0))
        for k in ("versions", "reports", "temporaries", "experiments", "ml_runs")
    )
    stats["trash_purged"] = purge_trash_if_due()  # hourly, even with the sweep switched off
    if any(stats.values()):
        log.info("reaper_cycle", **stats)
    return stats


def sweep_uploads(max_age_hours: float = 24.0) -> int:
    """Delete abandoned staged uploads: older than ``max_age_hours`` and owned by no live job.

    A directory named in a queued or running job's ``staged_dir`` is kept however old it
    is: a job can wait in the queue longer than a day, and deleting its files would turn
    it into "uploads no longer available" (#21). An upload still streaming has no job yet,
    so the age rule protects it.
    """
    import shutil
    from pathlib import Path

    root = get_settings().data_dir / "uploads"
    if not root.exists():
        return 0
    with SessionLocal() as db:
        live = {
            str(Path(p["staged_dir"]).resolve())
            for p in db.scalars(
                select(Job.payload).where(Job.status.in_((JobStatus.queued, JobStatus.running)))
            )
            if p and p.get("staged_dir")
        }
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for d in root.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff and str(d.resolve()) not in live:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed
