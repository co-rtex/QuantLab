"""Experiment artifact storage (Parquet + JSON) with staged, atomic publication."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import pandas as pd

from quantlab.backtest.engine import BacktestResult
from quantlab_api.config import get_settings


def experiment_dir(experiment_id: uuid.UUID | str) -> Path:
    return get_settings().artifacts_dir / "experiments" / str(uuid.UUID(str(experiment_id)))


def stage_dir(experiment_id: uuid.UUID | str, attempt: int) -> Path:
    d = get_settings().artifacts_dir / "staging" / f"{uuid.UUID(str(experiment_id))}-{attempt}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    return d


def write_backtest(staging: Path, res: BacktestResult, analysis: dict[str, Any]) -> None:
    eq = res.equity.copy()
    if res.benchmark is not None:
        eq["benchmark"] = res.benchmark.reindex(eq.index)
    eq.reset_index().to_parquet(staging / "equity.parquet", index=False)
    res.trades.to_parquet(staging / "trades.parquet", index=False)
    res.positions.to_parquet(staging / "positions.parquet", index=False)
    res.weights_target.to_parquet(staging / "weights.parquet", index=False)
    res.rejections.to_parquet(staging / "rejections.parquet", index=False)
    (staging / "analysis.json").write_text(json.dumps(analysis, default=str))


def publish(staging: Path, final: Path) -> None:
    """Replace ``final`` with ``staging``. Re-publication after a crash is idempotent."""
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        trash = final.with_name(final.name + f".old-{uuid.uuid4().hex[:8]}")
        os.rename(final, trash)
        shutil.rmtree(trash, ignore_errors=True)
    os.rename(staging, final)


def read_frame(experiment_id: uuid.UUID | str, name: str) -> pd.DataFrame:
    path = experiment_dir(experiment_id) / f"{name}.parquet"
    if not path.exists():
        raise FileNotFoundError(name)
    return pd.read_parquet(path)


def read_analysis(experiment_id: uuid.UUID | str) -> dict[str, Any]:
    return json.loads((experiment_dir(experiment_id) / "analysis.json").read_text())
