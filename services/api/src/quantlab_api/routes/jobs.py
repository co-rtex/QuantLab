from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from quantlab_api.errors import ApiError
from quantlab_api.jobs import request_cancel
from quantlab_api.models import Job, JobStatus, Role
from quantlab_api.schemas import MAX_OFFSET, JobOut, Page
from quantlab_api.security import DB, CurrentUser, audit, require_workspace

router = APIRouter(tags=["jobs"])


def load_job(db: DB, user: CurrentUser, job_id: uuid.UUID, min_role: Role = Role.viewer) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise ApiError(404, "not_found", "Job not found")
    require_workspace(db, user, job.workspace_id, min_role)
    return job


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, user: CurrentUser, db: DB) -> Job:
    return load_job(db, user, job_id)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: uuid.UUID, user: CurrentUser, db: DB, request: Request) -> Job:
    job = load_job(db, user, job_id, Role.editor)
    audit(
        db,
        request,
        "job.cancel",
        user=user,
        workspace_id=job.workspace_id,
        target_type="job",
        target_id=job.id,
    )
    return request_cancel(db, job)


@router.get("/workspaces/{workspace_id}/jobs", response_model=Page[JobOut])
def list_jobs(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    status: JobStatus | None = None,
    kind: str | None = Query(None, max_length=32),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
) -> Page[JobOut]:
    require_workspace(db, user, workspace_id)
    q = select(Job).where(Job.workspace_id == workspace_id)
    if status:
        q = q.where(Job.status == status)
    if kind:
        q = q.where(Job.kind == kind)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Job.created_at.desc()).limit(limit).offset(offset)).all()
    return Page(
        items=[JobOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )
