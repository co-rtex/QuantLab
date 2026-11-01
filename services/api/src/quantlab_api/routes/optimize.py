"""Portfolio construction research: compare allocators on point-in-time history.

``POST .../optimizations`` submits the computation as a worker job (#224) and ``GET
/optimizations/{id}`` reads it back. The synchronous ``POST .../optimize`` computes in this
process and stays until the web app has moved to the job (#224, part 2).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Header, Request
from sqlalchemy import select

from quantlab_api.config import get_settings
from quantlab_api.errors import ApiError
from quantlab_api.jobs import (
    enqueue,
    lock_idempotency_key,
    replayed_job,
    request_fingerprint,
    submit_job,
)
from quantlab_api.limits import heavy_slot
from quantlab_api.models import Instrument, Job, OptimizationRun, Role
from quantlab_api.optimization import OptimizeIn, OptimizeInputError, compute
from quantlab_api.routes.datasets import load_version
from quantlab_api.schemas import JobOut
from quantlab_api.security import (
    DB,
    CurrentUser,
    audit,
    login_limiter,
    rate_limited,
    require_workspace,
)
from quantlab_api.tasks import dataset_store

router = APIRouter(tags=["optimization"])
# Seconds per attempt; the largest request measured runs in a few seconds (#224).
OPTIMIZE_TIMEOUT_SECONDS = 300


@router.post("/dataset-versions/{version_id}/optimize")
def optimize(version_id: uuid.UUID, body: OptimizeIn, user: CurrentUser, db: DB) -> dict[str, Any]:
    """Allocations from returns dated on or before ``as_of`` only (no look-ahead).

    Computed in this process over each symbol's full history, so bounded (#223): per user
    and minute (429, the login limiter's fail-safe breaker included), and per process at
    once (503 server_busy with Retry-After, never queued)."""
    dv, ds = load_version(db, user, version_id)
    receipt = login_limiter.reserve_receipt(
        f"optimize:user:{user.id}", get_settings().rate_limit_optimize_per_minute
    )
    if not receipt:
        raise rate_limited(60, "Too many optimizations")
    try:
        with heavy_slot("optimize"):
            store = dataset_store()
            try:
                return compute(store, store.open(ds.slug, dv.version_hash), body, dv.is_synthetic)
            except OptimizeInputError as exc:
                raise ApiError(422, exc.code, exc.message, exc.details) from exc
    except ApiError as exc:
        if exc.code == "server_busy":  # refused before any work: not one of the user's 10
            login_limiter.refund(receipt)
        raise


def _run_out(db: DB, run: OptimizationRun) -> dict[str, Any]:
    job = db.get(Job, run.job_id) if run.job_id else None
    return {
        "id": str(run.id),
        "dataset_version_id": str(run.dataset_version_id),
        "status": run.status.value,
        "params": run.params,
        "code_version": run.code_version,
        "created_at": run.created_at,
        # The same document the synchronous route answers, once the job has completed.
        "result": run.result,
        "job": JobOut.model_validate(job).model_dump(mode="json") if job else None,
    }


@router.post("/dataset-versions/{version_id}/optimizations", status_code=202)
def submit_optimization(
    version_id: uuid.UUID,
    body: OptimizeIn,
    user: CurrentUser,
    db: DB,
    request: Request,
    idempotency_key: str | None = Header(None, min_length=1, max_length=128),
) -> dict[str, Any]:
    """Queue an optimization on a worker (#224). Editors only: a run and a job are written."""
    dv, ds = load_version(db, user, version_id)
    require_workspace(db, user, ds.workspace_id, Role.editor)
    params = body.model_dump(mode="json")
    fingerprint = request_fingerprint(f"optimize:{dv.id}", user.id, params)

    def previous(job: Job) -> OptimizationRun | None:
        run_id = (job.payload or {}).get("optimization_run_id")
        return db.get(OptimizationRun, uuid.UUID(run_id)) if run_id else None

    lock_idempotency_key(db, ds.workspace_id, idempotency_key)
    prior_job = replayed_job(db, ds.workspace_id, idempotency_key, fingerprint, lambda j: False)
    if prior_job is not None:  # the same request replayed: report it, run nothing
        prior = previous(prior_job)
        if prior is None:
            raise ApiError(
                409,
                "idempotency_conflict",
                "Idempotency-Key was already used with a different request",
            )
        return _run_out(db, prior)
    # Symbols the version doesn't list at all fail here, before a job; whether they have
    # data up to as_of is the job's to say. A version without catalog rows is not checked.
    listed = set(
        db.scalars(select(Instrument.symbol).where(Instrument.dataset_version_id == dv.id))
    )
    unknown = {s.upper() for s in body.symbols} - listed if listed else set()
    if unknown:
        raise ApiError(
            422, "unknown_symbols", "Symbols not in this dataset version", sorted(unknown)[:20]
        )
    receipt = login_limiter.reserve_receipt(
        f"optimize:user:{user.id}", get_settings().rate_limit_optimize_per_minute
    )
    if not receipt:
        raise rate_limited(60, "Too many optimizations")
    run = OptimizationRun(
        workspace_id=ds.workspace_id,
        dataset_version_id=dv.id,
        params=params,
        code_version=get_settings().git_sha,
        created_by=user.id,
    )
    db.add(run)
    db.flush()
    audit(
        db,
        request,
        "optimization.submitted",
        user=user,
        workspace_id=ds.workspace_id,
        target_type="optimization_run",
        target_id=run.id,
    )
    try:
        job, _ = submit_job(
            db,
            workspace_id=ds.workspace_id,
            user_id=user.id,
            kind="optimize",
            payload={"optimization_run_id": str(run.id), "request_fingerprint": fingerprint},
            idempotency_key=idempotency_key,
            request_id=getattr(request.state, "request_id", None),
            timeout_seconds=OPTIMIZE_TIMEOUT_SECONDS,
            dispatch=False,
            on_created=lambda j: setattr(run, "job_id", j.id),
        )
    except ApiError:  # refused (too_many_jobs): nothing queued, so not one of the user's 10
        login_limiter.refund(receipt)
        raise
    enqueue(job)
    return _run_out(db, run)


@router.get("/optimizations/{optimization_id}")
def get_optimization(optimization_id: uuid.UUID, user: CurrentUser, db: DB) -> dict[str, Any]:
    run = db.get(OptimizationRun, optimization_id)
    if run is None:
        raise ApiError(404, "not_found", "Optimization not found")
    try:
        require_workspace(db, user, run.workspace_id)
    except ApiError as exc:  # a non-member can't tell a run exists
        raise ApiError(404, "not_found", "Optimization not found") from exc
    return _run_out(db, run)
