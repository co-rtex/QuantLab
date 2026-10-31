from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from quantlab.research.integrity import build_integrity_report
from quantlab_api import artifacts
from quantlab_api.errors import ApiError
from quantlab_api.jobs import (
    advisory_lock,
    lock_idempotency_key,
    payload_hash,
    replayed_job,
    request_fingerprint,
    submit_job,
)
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
    idempotency_key: str | None = Header(None, min_length=1, max_length=128),
) -> dict[str, Any]:
    exp = load_experiment(db, user, experiment_id, Role.editor)
    if exp.status != JobStatus.completed:
        raise ApiError(409, "not_ready", "Run validation after the backtest completes")
    params = {"suite_version": SUITE_VERSION, "seed": 0}
    phash = payload_hash(SUITE, params)
    lock_idempotency_key(db, exp.workspace_id, idempotency_key)
    # Validation is deduplicated per experiment, not per key: without this, two requests
    # with no key (or different keys) both inserted a run (unique violation, 500) or both
    # requeued a failed one (two jobs for one run).
    advisory_lock(db, f"validation:{exp.id}")
    run = db.scalar(
        select(ResearchRun).where(
            ResearchRun.experiment_id == exp.id,
            ResearchRun.kind == SUITE,
            ResearchRun.params_hash == phash,
        )
    )
    fingerprint = request_fingerprint(f"validation:{exp.id}", user.id, params)

    def legacy_match(job: Job) -> bool:  # jobs from before request fingerprints
        return run is not None and (job.payload or {}).get("research_run_id") == str(run.id)

    # The key is checked before the per-experiment shortcut below, so a key that belongs to
    # another request is a conflict even when this experiment already has a run (#43).
    prior = replayed_job(db, exp.workspace_id, idempotency_key, fingerprint, legacy_match)
    if prior is not None:  # the same request replayed: report it, rerun nothing
        prior_run = db.get(ResearchRun, uuid.UUID(prior.payload["research_run_id"])) or run
        if prior_run is None:
            raise ApiError(
                409,
                "idempotency_conflict",
                "Idempotency-Key was already used with a different request",
            )
        return _run_out(db, prior_run)
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
        payload={"research_run_id": str(run.id), "seed": 0, "request_fingerprint": fingerprint},
        idempotency_key=idempotency_key,
        request_id=getattr(request.state, "request_id", None),
        timeout_seconds=1800,
        dispatch=False,
        on_created=lambda j: setattr(run, "job_id", j.id),
    )
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


@router.get("/experiments/{experiment_id}/report")
def report(
    experiment_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    request: Request,
    format: Literal["html", "md"] = "html",
    download: bool = False,
) -> Response:
    """Export a research report generated from persisted results."""
    from quantlab.reports.render import render_html, render_markdown, report_context
    from quantlab_api.routes.experiments import equity

    exp = load_experiment(db, user, experiment_id)
    if exp.status != JobStatus.completed:
        raise ApiError(409, "not_ready", "Reports need a completed backtest")
    ev = integrity_evidence(db, user, exp)
    ctx = report_context(
        experiment={
            **ev["experiment"],
            "name": exp.name,
            "strategy_key": exp.strategy_key,
            "strategy_params": exp.strategy_params,
        },
        dataset=ev["dataset"],
        analysis=ev["analysis"],
        equity=equity(exp.id, user, db, max_points=600),
        integrity=build_integrity_report(ev),
        validation=ev["validation"],
    )
    audit(
        db,
        request,
        "report.export",
        user=user,
        workspace_id=exp.workspace_id,
        target_type="experiment",
        target_id=exp.id,
        format=format,
    )
    db.commit()
    body = render_html(ctx) if format == "html" else render_markdown(ctx)
    media = "text/html; charset=utf-8" if format == "html" else "text/markdown; charset=utf-8"
    # The report is generated HTML that quotes user-supplied names and notes. Even if
    # escaping failed somewhere, the sandbox (no scripts, a unique origin, so no access to
    # the app's cookies or storage), no forms and no <base> keep it inert.
    headers = {
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
        "img-src data:; frame-ancestors 'none'; sandbox; form-action 'none'; base-uri 'none'"
    }
    if download:
        ext = "html" if format == "html" else "md"
        headers["Content-Disposition"] = f'attachment; filename="quantlab-report-{exp.id}.{ext}"'
    return Response(body, media_type=media, headers=headers)


class AssistantIn(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


def _provider() -> Any:
    from quantlab.assistant.service import AnthropicProvider, ProviderError
    from quantlab_api.config import get_settings

    s = get_settings()
    if s.anthropic_api_key is None or not s.anthropic_api_key.get_secret_value():
        return None
    try:
        return AnthropicProvider(s.anthropic_api_key.get_secret_value(), s.llm_model)
    except ProviderError:
        return None


@router.post("/experiments/{experiment_id}/assistant")
def assistant(
    experiment_id: uuid.UUID, body: AssistantIn, user: CurrentUser, db: DB
) -> dict[str, Any]:
    """Answer a question using only this experiment's computed evidence."""
    from quantlab.assistant.grounding import build_facts
    from quantlab.assistant.service import answer_question, facts_payload
    from quantlab_api.security import login_limiter

    exp = load_experiment(db, user, experiment_id)
    if exp.status != JobStatus.completed:
        raise ApiError(409, "not_ready", "The assistant needs a completed backtest")
    login_limiter.check(f"assistant:{user.id}", 30)
    ev = integrity_evidence(db, user, exp)
    facts = build_facts(
        ev["analysis"], build_integrity_report(ev), ev["validation"], exp.config.get("benchmark")
    )
    ans = answer_question(body.question, facts, _provider())
    cited = [f for f in facts_payload(facts) if f["id"] in set(ans.citations)]
    return {**ans.to_dict(), "facts": cited, "available_facts": len(facts)}
