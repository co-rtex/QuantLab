from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import pandas as pd
from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from quantlab.ml.pipeline import ML_PIPELINE_VERSION, MODELS, MLConfig
from quantlab_api import artifacts, provenance
from quantlab_api.config import get_settings
from quantlab_api.errors import ApiError
from quantlab_api.jobs import (
    enqueue,
    lock_idempotency_key,
    replayed_job,
    request_fingerprint,
    submit_job,
)
from quantlab_api.models import Job, MLRun, Role
from quantlab_api.routes.datasets import load_version
from quantlab_api.schemas import MAX_OFFSET, JobOut
from quantlab_api.security import DB, CurrentUser, audit, require_workspace

router = APIRouter(tags=["ml"])


class MLRunIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    dataset_version_id: uuid.UUID
    config: dict[str, Any] = Field(default_factory=dict)


def _out(
    db: DB, run: MLRun, full: bool = True, synthetic: dict[uuid.UUID, bool | None] | None = None
) -> dict[str, Any]:
    job = db.get(Job, run.job_id) if run.job_id else None
    flags = synthetic or provenance.synthetic_flags(db, [run.dataset_version_id])
    out: dict[str, Any] = {
        "id": str(run.id),
        "name": run.name,
        "dataset_version_id": str(run.dataset_version_id),
        # Additive (#102): from the dataset version; None when it is missing.
        "is_synthetic": flags.get(run.dataset_version_id),
        "config": run.config,
        "status": run.status.value,
        "input_hash": run.input_hash,
        "code_version": run.code_version,
        "created_at": run.created_at,
        "job": JobOut.model_validate(job).model_dump(mode="json") if job else None,
    }
    if full:
        out["result"] = run.result
    elif run.result:
        out["summary"] = run.result.get("summary")
    return out


def _load(db: DB, user: CurrentUser, run_id: uuid.UUID) -> MLRun:
    run = db.get(MLRun, run_id)
    if run is None:
        raise ApiError(404, "not_found", "ML run not found")
    require_workspace(db, user, run.workspace_id)
    return run


@router.post("/workspaces/{workspace_id}/ml-runs", status_code=202)
def create_ml_run(
    workspace_id: uuid.UUID,
    body: MLRunIn,
    user: CurrentUser,
    db: DB,
    request: Request,
    idempotency_key: str | None = Header(None, min_length=1, max_length=128),
) -> dict[str, Any]:
    require_workspace(db, user, workspace_id, Role.editor)
    dv, ds = load_version(db, user, body.dataset_version_id)
    if ds.workspace_id != workspace_id:
        raise ApiError(422, "validation_error", "dataset version belongs to another workspace")
    try:
        cfg = MLConfig(**body.config)
    except ValidationError as exc:
        raise ApiError(
            422,
            "invalid_config",
            "Invalid ML configuration",
            [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
        ) from exc
    if set(cfg.models) - set(MODELS):
        raise ApiError(422, "invalid_config", f"models must be among {list(MODELS)}")
    doc = cfg.model_dump(mode="json")
    ihash = hashlib.sha256(
        # The pipeline version too: the same dataset and config give a different panel
        # under a new pipeline (#120), so they must not share an input hash.
        json.dumps(
            {"dataset": dv.version_hash, "config": doc, "pipeline": ML_PIPELINE_VERSION},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    fingerprint = request_fingerprint(
        f"ml.create:{workspace_id}", user.id, body.model_dump(mode="json")
    )

    def previous(job: Job) -> MLRun | None:
        # By the payload's run id too: a job from before #40 may never have been linked.
        run_id = (job.payload or {}).get("ml_run_id")
        linked = db.scalar(select(MLRun).where(MLRun.job_id == job.id))
        return linked or (db.get(MLRun, uuid.UUID(run_id)) if run_id else None)

    def legacy_match(job: Job) -> bool:  # jobs from before request fingerprints
        prev = previous(job)
        return prev is not None and prev.input_hash == ihash

    lock_idempotency_key(db, workspace_id, idempotency_key)
    prior_job = replayed_job(db, workspace_id, idempotency_key, fingerprint, legacy_match)
    if prior_job is not None:
        prior = previous(prior_job)
        if prior is None:
            raise ApiError(
                409,
                "idempotency_conflict",
                "Idempotency-Key was already used with a different request",
            )
        return _out(db, prior)
    run = MLRun(
        workspace_id=workspace_id,
        name=body.name,
        dataset_version_id=dv.id,
        config=doc,
        input_hash=ihash,
        code_version=get_settings().git_sha,
        created_by=user.id,
    )
    db.add(run)
    db.flush()
    audit(
        db,
        request,
        "ml.run_submitted",
        user=user,
        workspace_id=workspace_id,
        target_type="ml_run",
        target_id=run.id,
    )
    job, _ = submit_job(
        db,
        workspace_id=workspace_id,
        user_id=user.id,
        kind="ml.experiment",
        payload={"ml_run_id": str(run.id), "request_fingerprint": fingerprint},
        idempotency_key=idempotency_key,
        request_id=getattr(request.state, "request_id", None),
        timeout_seconds=1800,
        dispatch=False,
        on_created=lambda j: setattr(run, "job_id", j.id),
    )
    enqueue(job)
    return _out(db, run)


@router.get("/workspaces/{workspace_id}/ml-runs")
def list_ml_runs(
    workspace_id: uuid.UUID, user: CurrentUser, db: DB, limit: int = Query(50, ge=1, le=200)
) -> list[dict[str, Any]]:
    require_workspace(db, user, workspace_id)
    rows = db.scalars(
        select(MLRun)
        .where(MLRun.workspace_id == workspace_id)
        .order_by(MLRun.created_at.desc())
        .limit(limit)
    ).all()
    flags = provenance.synthetic_flags(db, (r.dataset_version_id for r in rows))
    return [_out(db, r, full=False, synthetic=flags) for r in rows]


@router.get("/ml-runs/{run_id}")
def get_ml_run(run_id: uuid.UUID, user: CurrentUser, db: DB) -> dict[str, Any]:
    return _out(db, _load(db, user, run_id))


@router.get("/ml-runs/{run_id}/predictions")
def ml_predictions(
    run_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    model: str = Query("logistic", max_length=32),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
) -> dict[str, Any]:
    run = _load(db, user, run_id)
    path = artifacts.ml_dir(run.id) / "predictions.parquet"
    if not path.exists():
        raise ApiError(409, "not_ready", "Predictions are not available yet")
    p = pd.read_parquet(path)
    p = p[p["model"] == model]
    page = p.iloc[offset : offset + limit].copy()
    page["date"] = pd.to_datetime(page["date"]).dt.strftime("%Y-%m-%d")
    return {"items": page.to_dict("records"), "total": len(p), "limit": limit, "offset": offset}
