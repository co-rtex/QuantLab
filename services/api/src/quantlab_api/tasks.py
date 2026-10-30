"""Job handlers. Each returns a finalizer that writes results inside the commit transaction."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from quantlab.analytics.summary import analyze_backtest
from quantlab.backtest.engine import BacktestConfig, BacktestResult, run_backtest
from quantlab.data.quality import assess_quality
from quantlab.data.store import DatasetStore
from quantlab.data.synthetic import SyntheticSpec, generate_market
from quantlab.strategies.library import create_strategy
from quantlab_api import artifacts
from quantlab_api.config import get_settings
from quantlab_api.db import SessionLocal
from quantlab_api.jobs import Finalizer, JobContext, PermanentJobError, handler
from quantlab_api.models import (
    Dataset,
    DatasetVersion,
    Experiment,
    ExperimentResult,
    Instrument,
)
from quantlab_api.observability import DATASET_FRESHNESS_DAYS, DATASET_ROWS, INGEST_QUALITY


def dataset_store() -> DatasetStore:
    return DatasetStore(get_settings().datasets_dir)


def register_version(
    db: Session,
    dataset: Dataset,
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    provenance: dict[str, Any],
    instruments: pd.DataFrame | None,
) -> DatasetVersion:
    """Write an immutable dataset version and its catalog rows (idempotent by content)."""
    quality = assess_quality(bars, actions)
    dv = dataset_store().write(
        dataset.slug, bars, actions, provenance, quality.to_dict(), instruments
    )
    existing = db.scalar(
        select(DatasetVersion).where(
            DatasetVersion.dataset_id == dataset.id, DatasetVersion.version_hash == dv.version
        )
    )
    if existing is not None:
        return existing
    m = dv.manifest
    row = DatasetVersion(
        dataset_id=dataset.id,
        version_hash=dv.version,
        rows=m["rows"],
        symbol_count=len(m["symbols"]),
        start_date=pd.Timestamp(m["start"]).date() if m["start"] else None,
        end_date=pd.Timestamp(m["end"]).date() if m["end"] else None,
        is_synthetic=bool(provenance.get("is_synthetic")),
        quality_status=quality.status,
        provenance=provenance,
        quality=_quality_summary(quality.to_dict()),
        storage_path=str(dv.path),
    )
    db.add(row)
    db.flush()
    stats = bars.groupby("symbol")["date"].agg(["min", "max", "count"])
    meta = instruments.set_index("symbol") if instruments is not None else None
    last_session = bars["date"].max()
    for sym, st in stats.iterrows():
        info = meta.loc[sym] if meta is not None and sym in meta.index else None
        db.add(
            Instrument(
                dataset_version_id=row.id,
                symbol=str(sym),
                name=str(info["name"]) if info is not None else str(sym),
                sector=str(info["sector"]) if info is not None else None,
                asset_class=str(info["asset_class"]) if info is not None else "equity",
                first_date=st["min"].date(),
                last_date=st["max"].date(),
                delisted=bool(st["max"] < last_session),
                rows=int(st["count"]),
            )
        )
    # Aggregate gauges: tenant-controlled names never become metric labels.
    DATASET_ROWS.set(m["rows"])
    if m["end"]:
        DATASET_FRESHNESS_DAYS.set((datetime.now(UTC).date() - pd.Timestamp(m["end"]).date()).days)
    INGEST_QUALITY.labels(quality.status).inc()
    return row


def _quality_summary(q: dict[str, Any]) -> dict[str, Any]:
    out = dict(q)
    per = out.pop("per_symbol", [])
    out["per_symbol_worst"] = sorted(per, key=lambda p: p["coverage"])[:20]
    return out


@handler("dataset.synthetic")
def synthetic_dataset_task(ctx: JobContext) -> Finalizer:
    p = ctx.payload
    spec = SyntheticSpec(**p["spec"])
    if spec.n_assets > get_settings().max_dataset_assets:
        raise PermanentJobError("requested asset count exceeds the configured limit")
    ctx.heartbeat(0.05, "generating synthetic market", force=True)
    market = generate_market(spec)
    ctx.heartbeat(0.6, "validating and writing dataset version", force=True)
    dataset_id = uuid.UUID(p["dataset_id"])

    def finalize(db: Session) -> dict[str, Any]:
        ds = db.get(Dataset, dataset_id)
        if ds is None:
            raise PermanentJobError("dataset was deleted")
        dv = register_version(
            db, ds, market.bars, market.actions, market.provenance, market.instruments
        )
        return {
            "message": f"dataset version {dv.version_hash[:12]} ready",
            "dataset_version_id": str(dv.id),
        }

    return finalize


def load_experiment_inputs(
    db: Session, exp: Experiment
) -> tuple[pd.DataFrame, pd.DataFrame, BacktestConfig]:
    dv_row = db.get(DatasetVersion, exp.dataset_version_id)
    if dv_row is None:
        raise PermanentJobError("dataset version not found")
    ds = db.get(Dataset, dv_row.dataset_id)
    assert ds is not None
    store = dataset_store()
    dv = store.open(ds.slug, dv_row.version_hash)
    cfg = BacktestConfig(**exp.config)
    syms = None
    if cfg.universe:
        syms = [*cfg.universe, *([cfg.benchmark] if cfg.benchmark else [])]
    bars = store.load_bars(dv, symbols=syms, end=str(cfg.end) if cfg.end else None)
    actions = store.load_actions(dv, symbols=syms)
    return bars, actions, cfg


def run_experiment_backtest(
    exp: Experiment,
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    cfg: BacktestConfig,
    ctx: JobContext | None = None,
) -> BacktestResult:
    strategy = create_strategy(exp.strategy_key, exp.strategy_params)

    def progress(f: float) -> None:
        if ctx is not None:
            ctx.heartbeat(0.1 + 0.75 * f, f"simulating ({f:.0%})")

    return run_backtest(bars, actions, strategy, cfg, on_progress=progress)


@handler("backtest")
def backtest_task(ctx: JobContext) -> Finalizer:
    exp_id = uuid.UUID(ctx.payload["experiment_id"])
    with SessionLocal() as db:
        exp = db.get(Experiment, exp_id)
        if exp is None:
            raise PermanentJobError("experiment not found")
        ctx.heartbeat(0.02, "loading dataset", force=True)
        bars, actions, cfg = load_experiment_inputs(db, exp)
        db.expunge(exp)
    res = run_experiment_backtest(exp, bars, actions, cfg, ctx)
    ctx.heartbeat(0.88, "computing analytics", force=True)
    analysis = analyze_backtest(res)
    staging = artifacts.stage_dir(exp_id, ctx.attempt)
    artifacts.write_backtest(staging, res, analysis)
    fingerprint = res.fingerprint()
    perf = analysis["performance"]["values"]

    def finalize(db: Session) -> dict[str, Any]:
        final = artifacts.experiment_dir(exp_id)
        artifacts.publish(staging, final)
        db.execute(
            insert(ExperimentResult)
            .values(
                id=uuid.uuid4(),
                experiment_id=exp_id,
                job_id=ctx.job_id,
                metrics=analysis["performance"],
                summary=_headline(analysis),
                fingerprint=fingerprint,
                artifact_dir=str(final),
                created_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=["experiment_id"])
        )
        sharpe = perf.get("sharpe")
        return {"message": f"completed; Sharpe {sharpe:.2f}" if sharpe is not None else "completed"}

    return finalize


def _headline(a: dict[str, Any]) -> dict[str, Any]:
    p = a["performance"]["values"]
    keys = [
        "total_return",
        "cagr",
        "volatility",
        "sharpe",
        "sortino",
        "max_drawdown",
        "calmar",
        "beta",
        "alpha",
        "information_ratio",
        "tracking_error",
        "benchmark_total_return",
        "excess_total_return",
    ]
    out = {k: p.get(k) for k in keys}
    out.update(
        {
            "annual_turnover": a["costs"]["values"].get("annual_turnover"),
            "total_costs": a["costs"]["values"].get("total_costs"),
            "cost_drag_cagr": a["costs"]["values"].get("cost_drag_cagr"),
            "round_trips": a["trades"]["values"].get("round_trips"),
            "win_rate": a["trades"]["values"].get("win_rate"),
            "historical_var_95": a["risk"].get("historical_var"),
            "historical_es_95": a["risk"].get("historical_es"),
            "sessions": a["meta"].get("sessions"),
        }
    )
    return out


@handler("research.validation")
def validation_task(ctx: JobContext) -> Finalizer:
    from quantlab.research.suite import run_validation_suite
    from quantlab_api.models import ResearchRun

    run_id = uuid.UUID(ctx.payload["research_run_id"])
    with SessionLocal() as db:
        run = db.get(ResearchRun, run_id)
        if run is None:
            raise PermanentJobError("research run not found")
        exp = db.get(Experiment, run.experiment_id)
        if exp is None:
            raise PermanentJobError("experiment not found")
        ctx.heartbeat(0.01, "loading dataset", force=True)
        bars, actions, cfg = load_experiment_inputs(db, exp)
        key, params = exp.strategy_key, dict(exp.strategy_params)

    def progress(f: float, msg: str) -> None:
        ctx.heartbeat(0.02 + 0.95 * f, msg)

    result = run_validation_suite(
        bars, actions, key, params, cfg, on_progress=progress, seed=int(ctx.payload.get("seed", 0))
    )

    def finalize(db: Session) -> dict[str, Any]:
        row = db.get(ResearchRun, run_id)
        if row is None:
            raise PermanentJobError("research run was deleted")
        row.result = result
        wf = result.get("walk_forward") or {}
        oos = wf.get("mean_oos_sharpe")
        return {
            "message": f"validation complete; mean out-of-sample Sharpe {oos:.2f}"
            if oos is not None
            else "validation complete"
        }

    return finalize
