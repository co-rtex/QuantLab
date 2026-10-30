from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Header, Request
from sqlalchemy import func, select

from quantlab.research.integrity import build_integrity_report
from quantlab_api import artifacts
from quantlab_api.errors import ApiError
from quantlab_api.jobs import payload_hash, submit_job
from quantlab_api.models import (
    Experiment,
    ExperimentResult,
    Instrument,
    Job,
    JobStatus,
    ResearchRun,
    Role,
)
from quantlab_api.routes.datasets import load_version
from quantlab_api.routes.experiments import load_experiment, reproducibility
from quantlab_api.schemas import JobOut
from quantlab_api.security import DB, CurrentUser, audit

router = APIRouter(tags=["research"])
SUITE = "validation"
SUITE_VERSION = 1


def _run_out(db: DB, run: ResearchRun) -> dict[str, Any]:
    job = db.get(Job, run.job_id) if run.job_id else None
    return {
        "id": str(run.id),
        "kind": run.kind,
        "status": run.status.value,
        "params": run.params,
        "created_at": run.created_at,
        "result": run.result,
        "job": JobOut.model_validate(job).model_dump(mode="json") if job else None,
    }


@router.post("/experiments/{experiment_id}/validation", status_code=202)
def start_validation(
    experiment_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    request: Request,
    idempotency_key: str | None = Header(None, max_length=128),
) -> dict[str, Any]:
    exp = load_experiment(db, user, experiment_id, Role.editor)
    if exp.status != JobStatus.completed:
        raise ApiError(409, "not_ready", "Run validation after the backtest completes")
    params = {"suite_version": SUITE_VERSION, "seed": 0}
    phash = payload_hash(SUITE, params)
    run = db.scalar(
        select(ResearchRun).where(
            ResearchRun.experiment_id == exp.id,
            ResearchRun.kind == SUITE,
            ResearchRun.params_hash == phash,
        )
    )
    if run is not None and run.status in (JobStatus.queued, JobStatus.running, JobStatus.completed):
        return _run_out(db, run)
    if run is None:
        run = ResearchRun(experiment_id=exp.id, kind=SUITE, params=params, params_hash=phash)
        db.add(run)
        db.flush()
    else:  # a previous attempt failed or was cancelled: run it again
        run.status, run.result = JobStatus.queued, None
    audit(
        db,
        request,
        "research.validation_requested",
        user=user,
        workspace_id=exp.workspace_id,
        target_type="experiment",
        target_id=exp.id,
    )
    job, _ = submit_job(
        db,
        workspace_id=exp.workspace_id,
        user_id=user.id,
        kind="research.validation",
        payload={"research_run_id": str(run.id), "seed": 0},
        idempotency_key=idempotency_key,
        request_id=getattr(request.state, "request_id", None),
        timeout_seconds=1800,
        dispatch=False,
    )
    run.job_id = job.id
    db.commit()
    from quantlab_api.jobs import enqueue

    enqueue(job)
    return _run_out(db, run)


@router.get("/experiments/{experiment_id}/validation")
def get_validation(experiment_id: uuid.UUID, user: CurrentUser, db: DB) -> dict[str, Any]:
    exp = load_experiment(db, user, experiment_id)
    run = db.scalar(
        select(ResearchRun)
        .where(ResearchRun.experiment_id == exp.id, ResearchRun.kind == SUITE)
        .order_by(ResearchRun.created_at.desc())
    )
    if run is None:
        raise ApiError(404, "not_found", "No validation has been run for this experiment")
    return _run_out(db, run)


def integrity_evidence(db: DB, user: CurrentUser, exp: Experiment) -> dict[str, Any]:
    dv, _ = load_version(db, user, exp.dataset_version_id)
    res = db.scalar(select(ExperimentResult).where(ExperimentResult.experiment_id == exp.id))
    analysis = artifacts.read_analysis(exp.id) if res else None
    run = db.scalar(
        select(ResearchRun)
        .where(
            ResearchRun.experiment_id == exp.id,
            ResearchRun.kind == SUITE,
            ResearchRun.status == JobStatus.completed,
        )
        .order_by(ResearchRun.created_at.desc())
    )
    delisted = list(
        db.scalars(
            select(Instrument.symbol).where(
                Instrument.dataset_version_id == dv.id, Instrument.delisted.is_(True)
            )
        )
    )
    trials = (
        db.scalar(
            select(func.count())
            .select_from(Experiment)
            .where(
                Experiment.workspace_id == exp.workspace_id,
                Experiment.dataset_version_id == exp.dataset_version_id,
            )
        )
        or 0
    )
    repro = reproducibility(exp.id, user, db)
    return {
        "experiment": {
            "id": str(exp.id),
            "config": exp.config,
            "strategy": exp.strategy_key,
            "params": exp.strategy_params,
            "engine_version": exp.engine_version,
            "code_version": exp.code_version,
            "input_hash": exp.input_hash,
            "fingerprint": res.fingerprint if res else None,
        },
        "dataset": {
            "version_hash": dv.version_hash,
            "provenance": dv.provenance,
            "is_synthetic": dv.is_synthetic,
            "quality_status": dv.quality_status,
            "delisted_symbols": delisted,
        },
        "analysis": analysis,
        "validation": run.result if run else None,
        "reproductions": repro["comparisons"],
        "trials_on_dataset": trials,
    }


@router.get("/experiments/{experiment_id}/integrity")
def integrity(experiment_id: uuid.UUID, user: CurrentUser, db: DB) -> dict[str, Any]:
    exp = load_experiment(db, user, experiment_id)
    if exp.status != JobStatus.completed:
        raise ApiError(409, "not_ready", "The integrity report needs a completed backtest")
    report = build_integrity_report(integrity_evidence(db, user, exp))
    report["experiment_id"] = str(exp.id)
    return report
