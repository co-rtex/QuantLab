"""Job handlers. Each returns a finalizer that writes results inside the commit transaction."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from sqlalchemy import event, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from quantlab.analytics.summary import analyze_backtest
from quantlab.backtest.engine import BacktestConfig, BacktestResult, run_backtest
from quantlab.data.identity import (
    display_frame,
    is_legacy,
    key_by_instrument,
    label_ids,
    normalize_master,
    resolve_ids,
    resolve_universe,
    tickers_of,
)
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
    symbol_history: pd.DataFrame | None = None,
    master: pd.DataFrame | None = None,
) -> DatasetVersion:
    """Write an immutable dataset version and its catalog rows (idempotent by content).

    ``quality`` is the report from :func:`gate_quality`, which has already passed. A
    symbol history and instrument master, when given, are stored and hashed with the
    version (#9). The catalog has one row per (ticker, instrument), recording that
    instrument's delisting facts.
    """
    dv = dataset_store().write(
        dataset.slug,
        bars,
        actions,
        provenance,
        quality.to_dict(),
        instruments,
        symbol_history=symbol_history,
        master=master,
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
    # One catalog row per (ticker, instrument). Each bar is resolved as of its own date, so
    # a reused ticker's earlier holder keeps its own row, dates, delisting and sector (#66).
    keyed = pd.DataFrame(
        {"symbol": bars["symbol"].astype(str).to_numpy(), "date": bars["date"].to_numpy()}
    )
    if symbol_history is not None:
        ids = resolve_ids(symbol_history, keyed["symbol"], keyed["date"])
        keyed["iid"] = [None if i is None else str(i) for i in ids]
    else:
        keyed["iid"] = None
    stats = keyed.groupby(["symbol", "iid"], dropna=False)["date"].agg(["min", "max", "count"])
    last_session = keyed["date"].max()
    # Delisted means the *instrument* stopped trading: a renamed security's old ticker
    # ends early but the security has not delisted.
    instrument_last = keyed.dropna(subset=["iid"]).groupby("iid")["date"].max()
    # The ticker's holder on its last bar: ticker-keyed metadata describes that holder
    # only, so it is never copied onto an earlier, unrelated company.
    latest = stats["max"].reset_index().sort_values("max").groupby("symbol").tail(1)
    current = {
        (s, None if pd.isna(i) else str(i))
        for s, i in zip(latest["symbol"], latest["iid"], strict=True)
    }
    meta_by_id: dict[str, dict[str, Any]] = {}
    meta_by_symbol: dict[str, dict[str, Any]] = {}
    if instruments is not None:
        records = instruments.to_dict("records")
        meta_by_symbol = {str(r["symbol"]): r for r in records}
        if "instrument_id" in instruments.columns:
            meta_by_id = {
                str(r["instrument_id"]): r for r in records if pd.notna(r["instrument_id"])
            }
    facts = normalize_master(master).set_index("instrument_id") if master is not None else None
    for (sym, iid), st in stats.iterrows():
        iid = None if pd.isna(iid) else str(iid)
        info = meta_by_id.get(iid) if iid is not None else None
        if info is None and (sym, iid) in current:
            info = meta_by_symbol.get(str(sym))
        fact = facts.loc[iid] if facts is not None and iid in facts.index else None
        last = instrument_last[iid] if iid is not None else st["max"]
        db.add(
            Instrument(
                dataset_version_id=row.id,
                symbol=str(sym),
                name=str(info["name"]) if info is not None else str(sym),
                sector=str(info["sector"]) if info is not None else None,
                # Without metadata (e.g. a reused ticker's earlier holder known only by
                # ticker) this is the catalog's default, "equity", as for every such row.
                # A distinct "unknown" would change the ML and factor filters (#87 review).
                asset_class=str(info["asset_class"]) if info is not None else "equity",
                first_date=st["min"].date(),
                last_date=st["max"].date(),
                delisted=bool(last < last_session),
                rows=int(st["count"]),
                instrument_id=iid,
                delisting_date=(
                    fact["delisting_date"].date()
                    if fact is not None and pd.notna(fact["delisting_date"])
                    else None
                ),
                delisting_reason=(
                    str(fact["delisting_reason"])
                    if fact is not None and fact["delisting_reason"] is not None
                    else None
                ),
                delisting_return=(
                    float(fact["delisting_return"])
                    if fact is not None and pd.notna(fact["delisting_return"])
                    else None
                ),
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
            symbol_history=market.symbol_history,
            master=market.master,
        )
        return {
            "message": f"dataset version {dv.version_hash[:12]} ready",
            "dataset_version_id": str(dv.id),
        }

    return finalize


@dataclass(frozen=True)
class ExperimentInputs:
    """What a job simulates: bars and actions keyed by instrument, and the config with
    its universe and benchmark resolved to instrument ids. ``history`` turns results back
    into tickers (``display``); ``master`` carries delisting dates and returns."""

    bars: pd.DataFrame
    actions: pd.DataFrame
    cfg: BacktestConfig
    history: pd.DataFrame
    master: pd.DataFrame | None
    start: pd.Timestamp | None = None  # the date universe tickers were resolved on

    @property
    def keyed(self) -> bool:
        return not is_legacy(self.history)

    def display(self, frame: pd.DataFrame, date_col: str = "date") -> pd.DataFrame:
        """``symbol`` as the ticker valid on each row's date, plus ``instrument_id``."""
        return display_frame(frame, self.history, date_col)


def load_experiment_inputs(db: Session, exp: Experiment) -> ExperimentInputs:
    dv_row = db.get(DatasetVersion, exp.dataset_version_id)
    if dv_row is None:
        raise PermanentJobError("dataset version not found")
    ds = db.get(Dataset, dv_row.dataset_id)
    assert ds is not None
    store = dataset_store()
    dv = store.open(ds.slug, dv_row.version_hash)
    cfg = BacktestConfig(**exp.config)
    history = store.load_symbol_history(dv)
    master = store.load_master(dv)
    end = str(cfg.end) if cfg.end else None
    if is_legacy(history):  # ticker == instrument: nothing to resolve
        syms = (
            [*cfg.universe, *([cfg.benchmark] if cfg.benchmark else [])] if cfg.universe else None
        )
        bars = store.load_bars(dv, symbols=syms, end=end)
        return ExperimentInputs(bars, store.load_actions(dv, symbols=syms), cfg, history, master)
    # Tickers in the config name instruments as of the backtest start (point in time): a
    # security renamed later stays in, and a later reuse of the ticker is not picked up.
    # A start before the data (a holiday, or 2010 on 2015+ data) means the first session.
    first = pd.Timestamp(dv.manifest["start"])
    as_of = max(pd.Timestamp(cfg.start), first) if cfg.start else first
    until = pd.Timestamp(cfg.end or dv.manifest["end"])
    window = f"between {as_of.date()} and {until.date()}"
    ids: list[str] | None = None
    if cfg.universe:
        ids, unresolved = resolve_universe(history, cfg.universe, as_of, until)
        if unresolved:
            raise PermanentJobError(
                f"tickers not listed {window} in this dataset: {unresolved[:10]}"
            )
    bench = None
    if cfg.benchmark:
        found, unresolved = resolve_universe(history, [cfg.benchmark], as_of, until)
        if unresolved:
            raise PermanentJobError(f"benchmark {cfg.benchmark} not listed {window}")
        bench = found[0]
    syms = tickers_of(history, [*ids, *([bench] if bench else [])]) if ids else None
    bars, actions = key_by_instrument(
        store.load_bars(dv, symbols=syms, end=end), store.load_actions(dv, symbols=syms), history
    )
    if master is not None:  # only the loaded instruments, so "unmatched" means a real mismatch
        master = master[master["instrument_id"].isin(set(bars["symbol"]))]
    cfg = cfg.model_copy(update={"universe": ids, "benchmark": bench})
    return ExperimentInputs(bars, actions, cfg, history, master, as_of)


def run_experiment_backtest(
    exp: Experiment, inputs: ExperimentInputs, ctx: JobContext | None = None
) -> BacktestResult:
    strategy = create_strategy(exp.strategy_key, exp.strategy_params)

    def progress(f: float) -> None:
        if ctx is not None:
            ctx.heartbeat(0.1 + 0.75 * f, f"simulating ({f:.0%})")

    return run_backtest(
        inputs.bars,
        inputs.actions,
        strategy,
        inputs.cfg,
        on_progress=progress,
        instruments=inputs.master,
    )


def _factors(exp: Experiment, inputs: ExperimentInputs) -> pd.DataFrame | None:
    """Market and sector factors when the dataset carries sector metadata."""
    from quantlab.analytics.factors import sector_factors

    if not inputs.cfg.benchmark:
        return None
    with SessionLocal() as db:
        rows = db.execute(
            select(Instrument.symbol, Instrument.instrument_id, Instrument.sector).where(
                Instrument.dataset_version_id == exp.dataset_version_id,
                Instrument.sector.is_not(None),
            )
        ).all()
    if not rows:
        return None
    # Series are keyed by instrument when the dataset has identity data.
    sectors = pd.Series({(iid if inputs.keyed and iid else s): sec for s, iid, sec in rows})
    try:
        return sector_factors(inputs.bars, inputs.actions, sectors, inputs.cfg.benchmark.upper())
    except ValueError:
        return None


def _for_display(
    res: BacktestResult, analysis: dict[str, Any], inputs: ExperimentInputs
) -> tuple[BacktestResult, dict[str, Any]]:
    """Results as users see them: tickers in ``symbol``, the permanent id beside it.

    Simulation, fingerprints and stored metrics stay keyed by instrument; only the frames
    and symbol-keyed analysis entries written for the API are relabelled.
    """
    shown = replace(
        res,
        trades=inputs.display(res.trades),
        positions=inputs.display(res.positions),
        weights_target=inputs.display(res.weights_target),
        rejections=inputs.display(res.rejections),
    )
    if not inputs.keyed:
        return shown, analysis
    out = dict(analysis)
    # Holdings and P&L are labelled as of the last session, never with a later ticker. If
    # two instruments show the same ticker then (one delisted, one renamed into it), the
    # id disambiguates.
    final = res.equity.index[-1]
    contrib = dict(analysis.get("contributions") or {})
    held = list(analysis.get("final_holdings", {}))
    rows = [r["symbol"] for side in ("top", "bottom") for r in contrib.get(side, [])]
    name = _unique_labels(label_ids(inputs.history, sorted({*held, *rows}), final))

    out["final_holdings"] = {name(k): v for k, v in analysis.get("final_holdings", {}).items()}
    for side in ("top", "bottom"):
        contrib[side] = [
            {**r, "symbol": name(r["symbol"]), "instrument_id": r["symbol"]}
            for r in contrib.get(side, [])
        ]
    out["contributions"] = contrib
    # The run's universe as the user named it (tickers on the start date), ids beside it.
    meta = dict(analysis.get("meta") or {})
    when = inputs.start or final
    universe = [str(s) for s in meta.get("universe", [])]
    absent = [str(s) for s in meta.get("not_listed_in_window", [])]
    named = _unique_labels(label_ids(inputs.history, [*universe, *absent], when))
    meta["universe_instrument_ids"] = universe
    meta["universe"] = [named(i) for i in universe]
    meta["not_listed_in_window"] = [named(i) for i in absent]
    if meta.get("benchmark"):
        meta["benchmark_instrument_id"] = meta["benchmark"]
        meta["benchmark"] = label_ids(inputs.history, [meta["benchmark"]], when)[meta["benchmark"]]
    out["meta"] = meta
    return shown, out


def _unique_labels(labels: dict[str, str]) -> Callable[[str], str]:
    """Label lookup where a ticker shown for two instruments gets the id beside it."""
    shown = list(labels.values())
    clash = {t for t in shown if shown.count(t) > 1}

    def name(iid: str) -> str:
        t = labels.get(iid, iid)
        return f"{t} [{iid}]" if t in clash else t

    return name


@handler("backtest")
def backtest_task(ctx: JobContext) -> Finalizer:
    exp_id = uuid.UUID(ctx.payload["experiment_id"])
    with SessionLocal() as db:
        exp = db.get(Experiment, exp_id)
        if exp is None:
            raise PermanentJobError("experiment not found")
        ctx.heartbeat(0.02, "loading dataset", force=True)
        inputs = load_experiment_inputs(db, exp)
        db.expunge(exp)
    res = run_experiment_backtest(exp, inputs, ctx)
    ctx.heartbeat(0.88, "computing analytics", force=True)
    analysis = analyze_backtest(res, _factors(exp, inputs))
    staging = artifacts.stage_dir(exp_id, ctx.attempt)
    shown, analysis = _for_display(res, analysis, inputs)
    artifacts.write_backtest(staging, shown, analysis)
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
        inputs = load_experiment_inputs(db, exp)
        key, params = exp.strategy_key, dict(exp.strategy_params)

    def progress(f: float, msg: str) -> None:
        ctx.heartbeat(0.02 + 0.95 * f, msg)

    result = run_validation_suite(
        inputs.bars,
        inputs.actions,
        key,
        params,
        inputs.cfg,
        on_progress=progress,
        seed=int(ctx.payload.get("seed", 0)),
        instruments=inputs.master,
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
    history = store.load_symbol_history(dv)
    bars, actions = store.load_bars(dv), store.load_actions(dv)
    if not is_legacy(history):  # learn per instrument, so a rename is one series
        bars, actions = key_by_instrument(bars, actions, history)
        # ``exclude`` names tickers (the benchmark index by default); the series are now
        # keyed by instrument, so exclude every instrument that ever held one of them.
        named = {str(t).upper() for t in cfg.exclude}
        held = history.loc[history["symbol"].isin(named), "instrument_id"]
        cfg = cfg.model_copy(update={"exclude": tuple(sorted(set(held)))})
    result = run_ml_experiment(
        bars,
        actions,
        cfg,
        on_progress=lambda f, m: ctx.heartbeat(0.05 + 0.9 * f, m),
        instruments=store.load_master(dv),
    )
    preds = display_frame(result.pop("predictions"), history)
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
