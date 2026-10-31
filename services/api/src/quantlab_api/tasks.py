"""Job handlers. Each returns a finalizer that writes results inside the commit transaction."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from sqlalchemy import event, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from quantlab.analytics.summary import analyze_backtest
from quantlab.backtest.engine import BacktestConfig, BacktestResult, run_backtest
from quantlab.data.quality import (
    QualityGateError,
    QualityReport,
    assess_quality,
    enforce_gates,
)
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


def gate_quality(bars: pd.DataFrame, actions: pd.DataFrame) -> QualityReport:
    """Run the quality checks; a failing ingestion gate fails the job permanently.

    Called in the job body, before ``finalize``, so a rejected dataset writes nothing and
    the CSV path's staged-file cleanup runs. Retrying cannot fix the data.
    """
    report = assess_quality(bars, actions)
    try:
        return enforce_gates(report)
    except QualityGateError as exc:
        raise PermanentJobError(str(exc)) from exc


def register_version(
    db: Session,
    dataset: Dataset,
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    provenance: dict[str, Any],
    instruments: pd.DataFrame | None,
    *,
    quality: QualityReport,
) -> DatasetVersion:
    """Write an immutable dataset version and its catalog rows (idempotent by content).

    ``quality`` is the report from :func:`gate_quality`, which has already passed.
    """
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
    quality = gate_quality(market.bars, market.actions)
    dataset_id = uuid.UUID(p["dataset_id"])

    def finalize(db: Session) -> dict[str, Any]:
        ds = db.get(Dataset, dataset_id)
        if ds is None:
            raise PermanentJobError("dataset was deleted")
        dv = register_version(
            db,
            ds,
            market.bars,
            market.actions,
            market.provenance,
            market.instruments,
            quality=quality,
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


def _factors(
    exp: Experiment, bars: pd.DataFrame, actions: pd.DataFrame, cfg: BacktestConfig
) -> pd.DataFrame | None:
    """Market and sector factors when the dataset carries sector metadata."""
    from quantlab.analytics.factors import sector_factors

    if not cfg.benchmark:
        return None
    with SessionLocal() as db:
        rows = db.execute(
            select(Instrument.symbol, Instrument.sector).where(
                Instrument.dataset_version_id == exp.dataset_version_id,
                Instrument.sector.is_not(None),
            )
        ).all()
    if not rows:
        return None
    sectors = pd.Series({s: sec for s, sec in rows})
    try:
        return sector_factors(bars, actions, sectors, cfg.benchmark.upper())
    except ValueError:
        return None


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
    analysis = analyze_backtest(res, _factors(exp, bars, actions, cfg))
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


@handler("ml.experiment")
def ml_task(ctx: JobContext) -> Finalizer:
    from quantlab.analytics.summary import jsonable
    from quantlab.ml.pipeline import MLConfig, run_ml_experiment
    from quantlab_api.models import MLRun

    run_id = uuid.UUID(ctx.payload["ml_run_id"])
    with SessionLocal() as db:
        run = db.get(MLRun, run_id)
        if run is None:
            raise PermanentJobError("ML run not found")
        dv_row = db.get(DatasetVersion, run.dataset_version_id)
        ds = db.get(Dataset, dv_row.dataset_id) if dv_row else None
        if dv_row is None or ds is None:
            raise PermanentJobError("dataset version not found")
        cfg = MLConfig(**run.config)
    ctx.heartbeat(0.02, "building features", force=True)
    store = dataset_store()
    dv = store.open(ds.slug, dv_row.version_hash)
    result = run_ml_experiment(
        store.load_bars(dv),
        store.load_actions(dv),
        cfg,
        on_progress=lambda f, m: ctx.heartbeat(0.05 + 0.9 * f, m),
    )
    preds = result.pop("predictions")
    staging = artifacts.stage_dir(run_id, ctx.attempt)
    preds.to_parquet(staging / "predictions.parquet", index=False)
    doc = jsonable(result)

    def finalize(db: Session) -> dict[str, Any]:
        row = db.get(MLRun, run_id)
        if row is None:
            raise PermanentJobError("ML run was deleted")
        artifacts.publish(staging, artifacts.ml_dir(run_id))
        row.result = doc
        best = max(
            (s for s in doc["summary"] if s.get("auc") is not None),
            key=lambda s: s["auc"],
            default=None,
        )
        return {
            "message": f"best out-of-sample AUC {best['auc']:.3f} ({best['model']})"
            if best
            else "completed"
        }

    return finalize


@handler("dataset.csv")
def csv_dataset_task(ctx: JobContext) -> Finalizer:
    """Validate and version an uploaded CSV dataset (staged by the upload endpoint)."""
    import shutil
    from pathlib import Path

    p = ctx.payload
    staged = Path(p["staged_dir"])
    if not staged.exists():
        raise PermanentJobError("uploaded files are no longer available; upload again")
    ctx.heartbeat(0.05, "parsing CSV", force=True)
    try:
        return _csv_finalizer(ctx, staged)
    except PermanentJobError:
        # Retrying cannot fix a bad file, so release the staged upload now.
        shutil.rmtree(staged, ignore_errors=True)
        raise


def _csv_finalizer(ctx: JobContext, staged: Any) -> Finalizer:
    import shutil

    from quantlab.data.schema import SchemaError, normalize_actions, normalize_bars

    p = ctx.payload
    try:
        bars = normalize_bars(pd.read_csv(staged / "bars.csv"))
        actions_path = staged / "actions.csv"
        actions = normalize_actions(pd.read_csv(actions_path) if actions_path.exists() else None)
    except (SchemaError, ValueError, pd.errors.ParserError) as exc:
        raise PermanentJobError(f"invalid CSV: {exc}") from exc
    if bars["symbol"].nunique() > get_settings().max_dataset_assets:
        raise PermanentJobError("too many symbols")
    dupes = int(bars.duplicated(["symbol", "date"]).sum())
    bars = bars.drop_duplicates(["symbol", "date"], keep="last")
    provenance = {
        "provider": "csv",
        "is_synthetic": False,
        "source": p["source"],
        "license": p["license"],
        "uploaded_files": p["files"],
        "duplicates_dropped": dupes,
    }
    ctx.heartbeat(0.5, "validating and writing dataset version", force=True)
    quality = gate_quality(bars, actions)
    dataset_id = uuid.UUID(p["dataset_id"])

    def finalize(db: Session) -> dict[str, Any]:
        ds = db.get(Dataset, dataset_id)
        if ds is None:
            raise PermanentJobError("dataset was deleted")
        dv = register_version(db, ds, bars, actions, provenance, None, quality=quality)
        # Release the upload only once this transaction has committed: deleting it here
        # turned a failed commit, which the job retries, into "files no longer available"
        # (#7). If the commit raises, the files stay for the retry (or the reaper's sweep).
        event.listen(
            db, "after_commit", lambda _s: shutil.rmtree(staged, ignore_errors=True), once=True
        )
        return {
            "message": f"dataset version {dv.version_hash[:12]} ready ({dv.quality_status})",
            "dataset_version_id": str(dv.id),
        }

    return finalize
