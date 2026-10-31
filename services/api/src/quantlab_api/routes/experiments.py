from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, Header, Query, Request
from pydantic import ValidationError
from sqlalchemy import func, select

from quantlab.backtest.engine import ENGINE_VERSION, BacktestConfig
from quantlab_api import artifacts
from quantlab_api.config import get_settings
from quantlab_api.errors import ApiError
from quantlab_api.jobs import (
    lock_idempotency_key,
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
    Role,
    StrategyDefinition,
    User,
)
from quantlab_api.routes.datasets import load_version
from quantlab_api.routes.strategies import validate_strategy
from quantlab_api.schemas import (
    MAX_OFFSET,
    BoundedDate,
    CompareIn,
    ExperimentIn,
    ExperimentOut,
    ExperimentSubmitted,
    JobOut,
    Page,
)
from quantlab_api.security import DB, CurrentUser, audit, require_workspace

router = APIRouter(tags=["experiments"])


def input_hash(version_hash: str, key: str, params: dict[str, Any], cfg: dict[str, Any]) -> str:
    doc = {
        "dataset": version_hash,
        "strategy": key,
        "params": params,
        "config": cfg,
        "engine": ENGINE_VERSION,
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()


def load_experiment(
    db: DB, user: User, exp_id: uuid.UUID, min_role: Role = Role.viewer
) -> Experiment:
    exp = db.get(Experiment, exp_id)
    if exp is None:
        raise ApiError(404, "not_found", "Experiment not found")
    require_workspace(db, user, exp.workspace_id, min_role)
    return exp


def to_out(exp: Experiment, result: ExperimentResult | None) -> ExperimentOut:
    out = ExperimentOut.model_validate(exp)
    if result is not None:
        out = out.model_copy(update={"summary": result.summary, "fingerprint": result.fingerprint})
    return out


def _result(db: DB, exp_id: uuid.UUID) -> ExperimentResult | None:
    return db.scalar(select(ExperimentResult).where(ExperimentResult.experiment_id == exp_id))


def _require_result(db: DB, exp: Experiment) -> ExperimentResult:
    r = _result(db, exp.id)
    if r is None:
        raise ApiError(409, "not_ready", f"Experiment is {exp.status.value}; no results yet")
    return r


def create_experiment(
    db: DB,
    user: User,
    workspace_id: uuid.UUID,
    body: ExperimentIn,
    request: Request | None,
    idempotency_key: str | None,
    parent_id: uuid.UUID | None = None,
) -> ExperimentSubmitted:
    dv, ds = load_version(db, user, body.dataset_version_id)
    if ds.workspace_id != workspace_id:
        raise ApiError(422, "validation_error", "dataset version belongs to another workspace")
    key, params = body.strategy_key, body.strategy_params
    if body.strategy_id:
        sd = db.get(StrategyDefinition, body.strategy_id)
        if sd is None or sd.workspace_id != workspace_id:
            raise ApiError(404, "not_found", "Strategy not found")
        key, params = sd.strategy_key, sd.params
    if not key:
        raise ApiError(422, "validation_error", "strategy_key or strategy_id is required")
    params = validate_strategy(key, params)
    try:
        cfg = BacktestConfig(**body.config)
    except ValidationError as exc:
        raise ApiError(
            422,
            "invalid_config",
            "Invalid backtest configuration",
            [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
        ) from exc
    symbols = set(
        db.scalars(select(Instrument.symbol).where(Instrument.dataset_version_id == dv.id))
    )
    wanted = [s.upper() for s in (cfg.universe or [])]
    missing = [
        s
        for s in [*wanted, *([cfg.benchmark.upper()] if cfg.benchmark else [])]
        if s not in symbols
    ]
    if missing:
        raise ApiError(422, "unknown_symbols", "Symbols not in dataset version", missing[:20])
    n_univ = len(wanted) if wanted else len(symbols)
    if n_univ > get_settings().max_backtest_symbols:
        raise ApiError(422, "universe_too_large", "Universe exceeds the configured limit")
    cfg_doc = cfg.model_dump(mode="json")
    exp = Experiment(
        workspace_id=workspace_id,
        name=body.name,
        kind="backtest",
        dataset_version_id=dv.id,
        strategy_key=key,
        strategy_params=params,
        config=cfg_doc,
        input_hash=input_hash(dv.version_hash, key, params, cfg_doc),
        code_version=get_settings().git_sha,
        engine_version=ENGINE_VERSION,
        parent_id=parent_id,
        notes=body.notes,
        created_by=user.id,
        status=JobStatus.queued,
    )
    endpoint = f"experiments.reproduce:{parent_id}" if parent_id else "experiments.create"
    fingerprint = request_fingerprint(endpoint, user.id, body.model_dump(mode="json"))

    def previous(job: Job) -> Experiment | None:
        prior_exp = (job.payload or {}).get("experiment_id")
        return db.get(Experiment, uuid.UUID(prior_exp)) if prior_exp else None

    def legacy_match(job: Job) -> bool:  # jobs from before request fingerprints
        prev = previous(job)
        return prev is not None and prev.input_hash == exp.input_hash

    lock_idempotency_key(db, workspace_id, idempotency_key)
    prior = replayed_job(db, workspace_id, idempotency_key, fingerprint, legacy_match)
    if prior is not None:
        prev = previous(prior)
        if prev is None:
            raise ApiError(
                409,
                "idempotency_conflict",
                "Idempotency-Key was already used with a different request",
            )
        return ExperimentSubmitted(
            experiment=to_out(prev, _result(db, prev.id)),
            job=JobOut.model_validate(prior),
            created=False,
        )
    db.add(exp)
    db.flush()
    audit(
        db,
        request,
        "experiment.submit",
        user=user,
        workspace_id=workspace_id,
        target_type="experiment",
        target_id=exp.id,
        strategy=key,
        parent=str(parent_id) if parent_id else None,
    )
    job, created = submit_job(
        db,
        workspace_id=workspace_id,
        user_id=user.id,
        kind="backtest",
        payload={"experiment_id": str(exp.id), "request_fingerprint": fingerprint},
        idempotency_key=idempotency_key,
        request_id=getattr(request.state, "request_id", None) if request else None,
    )
    return ExperimentSubmitted(
        experiment=to_out(exp, None), job=JobOut.model_validate(job), created=created
    )


@router.post(
    "/workspaces/{workspace_id}/experiments", response_model=ExperimentSubmitted, status_code=202
)
def submit_experiment(
    workspace_id: uuid.UUID,
    body: ExperimentIn,
    user: CurrentUser,
    db: DB,
    request: Request,
    idempotency_key: str | None = Header(None, min_length=1, max_length=128),
) -> ExperimentSubmitted:
    require_workspace(db, user, workspace_id, Role.editor)
    return create_experiment(db, user, workspace_id, body, request, idempotency_key)


@router.get("/workspaces/{workspace_id}/experiments", response_model=Page[ExperimentOut])
def list_experiments(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    status: JobStatus | None = None,
    strategy_key: str | None = Query(None, max_length=64),
    q: str | None = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
) -> Page[ExperimentOut]:
    require_workspace(db, user, workspace_id)
    stmt = select(Experiment).where(Experiment.workspace_id == workspace_id)
    if status:
        stmt = stmt.where(Experiment.status == status)
    if strategy_key:
        stmt = stmt.where(Experiment.strategy_key == strategy_key)
    if q:
        stmt = stmt.where(Experiment.name.ilike(f"%{q}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(
        stmt.add_columns(ExperimentResult)
        .outerjoin(ExperimentResult, ExperimentResult.experiment_id == Experiment.id)
        .order_by(Experiment.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return Page(items=[to_out(e, r) for e, r in rows], total=total, limit=limit, offset=offset)


@router.get("/experiments/{experiment_id}", response_model=ExperimentOut)
def get_experiment(experiment_id: uuid.UUID, user: CurrentUser, db: DB) -> ExperimentOut:
    exp = load_experiment(db, user, experiment_id)
    return to_out(exp, _result(db, exp.id))


@router.get("/experiments/{experiment_id}/job", response_model=JobOut)
def experiment_job(experiment_id: uuid.UUID, user: CurrentUser, db: DB) -> Job:
    exp = load_experiment(db, user, experiment_id)
    job = db.scalar(
        select(Job)
        .where(
            Job.workspace_id == exp.workspace_id,
            Job.kind == "backtest",
            Job.payload["experiment_id"].as_string() == str(exp.id),
        )
        .order_by(Job.created_at.desc())
    )
    if job is None:
        raise ApiError(404, "not_found", "No job for this experiment")
    return job


@router.get("/experiments/{experiment_id}/analysis")
def analysis(experiment_id: uuid.UUID, user: CurrentUser, db: DB) -> dict[str, Any]:
    exp = load_experiment(db, user, experiment_id)
    _require_result(db, exp)
    return artifacts.read_analysis(exp.id)


def _series(df: pd.DataFrame, cols: list[str], max_points: int) -> dict[str, Any]:
    if len(df) > max_points:
        step = int(np.ceil(len(df) / max_points))
        df = pd.concat([df.iloc[::step], df.iloc[[-1]]]).drop_duplicates(subset=["date"])
    out: dict[str, Any] = {"dates": [pd.Timestamp(d).strftime("%Y-%m-%d") for d in df["date"]]}
    for c in cols:
        if c in df:
            arr = df[c].to_numpy(dtype=float)
            out[c] = [None if not np.isfinite(v) else round(float(v), 6) for v in arr]
    return out


@router.get("/experiments/{experiment_id}/equity")
def equity(
    experiment_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    max_points: int = Query(2000, ge=10, le=20000),
) -> dict[str, Any]:
    exp = load_experiment(db, user, experiment_id)
    _require_result(db, exp)
    eq = artifacts.read_frame(exp.id, "equity")
    eq["drawdown"] = eq["equity"] / eq["equity"].cummax() - 1
    if "benchmark" in eq:
        eq["benchmark_drawdown"] = eq["benchmark"] / eq["benchmark"].cummax() - 1
    eq["exposure"] = eq["market_value"] / eq["equity"]
    r = eq["equity"].pct_change()
    eq["rolling_vol_63"] = r.rolling(63).std(ddof=1) * np.sqrt(252)
    eq["rolling_sharpe_126"] = r.rolling(126).mean() / r.rolling(126).std(ddof=1) * np.sqrt(252)
    cols = [
        "equity",
        "gross_equity",
        "benchmark",
        "cash",
        "drawdown",
        "benchmark_drawdown",
        "exposure",
        "turnover",
        "positions",
        "rolling_vol_63",
        "rolling_sharpe_126",
    ]
    return _series(eq, cols, max_points)


@router.get("/experiments/{experiment_id}/trades")
def trades(
    experiment_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    symbol: str | None = Query(None, max_length=32),
    side: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
) -> dict[str, Any]:
    exp = load_experiment(db, user, experiment_id)
    _require_result(db, exp)
    t = artifacts.read_frame(exp.id, "trades")
    if symbol and len(t):
        t = t[t["symbol"] == symbol.upper()]
    if side in ("buy", "sell") and len(t):
        t = t[t["side"] == side]
    total = len(t)
    page = t.iloc[offset : offset + limit].copy()
    for c in ("date", "decision_date"):
        if c in page:
            page[c] = pd.to_datetime(page[c]).dt.strftime("%Y-%m-%d")
    page = page.replace({np.nan: None})
    return {"items": page.to_dict("records"), "total": total, "limit": limit, "offset": offset}


@router.get("/experiments/{experiment_id}/positions")
def positions(
    experiment_id: uuid.UUID, user: CurrentUser, db: DB, on: BoundedDate | None = None
) -> dict[str, Any]:
    exp = load_experiment(db, user, experiment_id)
    _require_result(db, exp)
    p = artifacts.read_frame(exp.id, "positions")
    if len(p) == 0:
        return {"date": None, "items": [], "dates_available": 0}
    dates = pd.to_datetime(p["date"])
    target = pd.Timestamp(on) if on else dates.max()
    candidates = dates[dates <= target]
    if len(candidates) == 0:
        return {"date": None, "items": [], "dates_available": int(dates.nunique())}
    d = candidates.max()
    rows = p[dates == d].sort_values("value", ascending=False)
    return {
        "date": d.strftime("%Y-%m-%d"),
        "dates_available": int(dates.nunique()),
        "items": rows.drop(columns=["date"]).replace({np.nan: None}).to_dict("records"),
    }


@router.post("/workspaces/{workspace_id}/experiments/compare")
def compare(
    workspace_id: uuid.UUID,
    body: CompareIn,
    user: CurrentUser,
    db: DB,
    max_points: int = Query(1000, ge=10, le=5000),
) -> dict[str, Any]:
    require_workspace(db, user, workspace_id)
    out = []
    for eid in body.experiment_ids:
        exp = db.get(Experiment, eid)
        if exp is None or exp.workspace_id != workspace_id:
            raise ApiError(404, "not_found", f"Experiment {eid} not found")
        res = _result(db, eid)
        if res is None:
            raise ApiError(409, "not_ready", f"Experiment {exp.name} has no results")
        eq = artifacts.read_frame(eid, "equity")
        eq["normalized"] = eq["equity"] / eq["equity"].iloc[0]
        out.append(
            {
                "id": str(eid),
                "name": exp.name,
                "strategy_key": exp.strategy_key,
                "summary": res.summary,
                "series": _series(eq, ["normalized"], max_points),
            }
        )
    return {"experiments": out}


@router.post(
    "/experiments/{experiment_id}/reproduce", response_model=ExperimentSubmitted, status_code=202
)
def reproduce(
    experiment_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    request: Request,
    idempotency_key: str | None = Header(None, min_length=1, max_length=128),
) -> ExperimentSubmitted:
    exp = load_experiment(db, user, experiment_id, Role.editor)
    body = ExperimentIn(
        name=f"{exp.name} (reproduction)",
        dataset_version_id=exp.dataset_version_id,
        strategy_key=exp.strategy_key,
        strategy_params=exp.strategy_params,
        config=exp.config,
        notes=f"Reproduction of {exp.id}",
    )
    return create_experiment(
        db, user, exp.workspace_id, body, request, idempotency_key, parent_id=exp.id
    )


@router.get("/experiments/{experiment_id}/reproducibility")
def reproducibility(experiment_id: uuid.UUID, user: CurrentUser, db: DB) -> dict[str, Any]:
    """Compare this experiment's output fingerprint with its parent's (or vice versa)."""
    exp = load_experiment(db, user, experiment_id)
    res = _result(db, exp.id)
    related = []
    if exp.parent_id:
        related.append(exp.parent_id)
    related += list(db.scalars(select(Experiment.id).where(Experiment.parent_id == exp.id)))
    checks = []
    for rid in related:
        other = db.get(Experiment, rid)
        ores = _result(db, rid)
        if other is None:
            continue
        checks.append(
            {
                "experiment_id": str(rid),
                "name": other.name,
                "same_inputs": other.input_hash == exp.input_hash,
                "same_code_version": other.code_version == exp.code_version,
                "fingerprint_match": (
                    res is not None and ores is not None and res.fingerprint == ores.fingerprint
                ),
                "status": other.status.value,
            }
        )
    return {
        "experiment_id": str(exp.id),
        "input_hash": exp.input_hash,
        "fingerprint": res.fingerprint if res else None,
        "comparisons": checks,
    }


@router.get("/experiments/{experiment_id}/manifest")
def manifest(experiment_id: uuid.UUID, user: CurrentUser, db: DB) -> dict[str, Any]:
    """Everything needed to reproduce the experiment elsewhere."""
    exp = load_experiment(db, user, experiment_id)
    dv, ds = load_version(db, user, exp.dataset_version_id)
    res = _result(db, exp.id)
    return {
        "schema": "quantlab.experiment-manifest/v1",
        "experiment": {"id": str(exp.id), "name": exp.name, "created_at": exp.created_at},
        "dataset": {
            "slug": ds.slug,
            "version_hash": dv.version_hash,
            "provider": ds.provider,
            "is_synthetic": dv.is_synthetic,
            "provenance": dv.provenance,
        },
        "strategy": {"key": exp.strategy_key, "params": exp.strategy_params},
        "config": exp.config,
        "engine_version": exp.engine_version,
        "code_version": exp.code_version,
        "input_hash": exp.input_hash,
        "result_fingerprint": res.fingerprint if res else None,
    }
