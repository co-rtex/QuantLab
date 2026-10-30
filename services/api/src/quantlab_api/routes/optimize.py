"""Portfolio construction research: compare allocators on point-in-time history."""

from __future__ import annotations

import math
import uuid
from typing import Any, Literal

import numpy as np
import pandas as pd
from fastapi import APIRouter
from pydantic import BaseModel, Field

from quantlab.analytics.risk import concentration, covariance
from quantlab.analytics.summary import jsonable
from quantlab.data.adjust import total_returns
from quantlab.optimize.allocators import (
    Bounds,
    OptimizationError,
    equal_weight,
    mean_variance,
    min_variance,
    risk_contributions,
    risk_parity,
)
from quantlab_api.errors import ApiError
from quantlab_api.routes.datasets import load_version
from quantlab_api.schemas import BoundedDate
from quantlab_api.security import DB, CurrentUser
from quantlab_api.tasks import dataset_store

router = APIRouter(tags=["optimization"])
Method = Literal["equal_weight", "min_variance", "mean_variance", "risk_parity"]
ALL_METHODS: tuple[Method, ...] = ("equal_weight", "min_variance", "mean_variance", "risk_parity")


def _all_methods() -> list[Method]:
    return list(ALL_METHODS)


class OptimizeIn(BaseModel):
    symbols: list[str] = Field(min_length=2, max_length=100)
    as_of: BoundedDate | None = None
    lookback: int = Field(252, ge=40, le=1260)
    max_weight: float = Field(0.4, gt=0, le=1)
    risk_aversion: float = Field(5.0, gt=0, le=100)
    covariance: Literal["sample", "ledoit_wolf"] = "ledoit_wolf"
    benchmark: str | None = Field(None, max_length=32)
    methods: list[Method] = Field(default_factory=_all_methods, min_length=1, max_length=4)


@router.post("/dataset-versions/{version_id}/optimize")
def optimize(version_id: uuid.UUID, body: OptimizeIn, user: CurrentUser, db: DB) -> dict[str, Any]:
    """Allocations from returns dated on or before ``as_of`` only (no look-ahead)."""
    dv, ds = load_version(db, user, version_id)
    store = dataset_store()
    v = store.open(ds.slug, dv.version_hash)
    syms = sorted({s.upper() for s in body.symbols})
    bench = body.benchmark.upper() if body.benchmark else None
    bars = store.load_bars(
        v, symbols=[*syms, *([bench] if bench else [])], end=str(body.as_of) if body.as_of else None
    )
    missing = set(syms) - set(bars["symbol"].unique())
    if missing:
        raise ApiError(
            422, "unknown_symbols", "Symbols without data up to as_of", sorted(missing)[:20]
        )
    rets = total_returns(bars, store.load_actions(v, symbols=[*syms, *([bench] if bench else [])]))
    window = rets[syms].iloc[-body.lookback :]
    usable = [s for s in syms if window[s].notna().sum() >= int(0.9 * body.lookback)]
    if len(usable) < 2:
        raise ApiError(
            422,
            "insufficient_history",
            "Fewer than two symbols have enough history in the look-back window",
        )
    window = window[usable].dropna()
    cov = covariance(window, method=body.covariance).to_numpy()
    mu = window.mean().to_numpy() * 252
    n = len(usable)
    cap = max(body.max_weight, 1.0 / n)
    betas = None
    if bench and bench in rets:
        b = rets[bench].reindex(window.index)
        betas = window.apply(
            lambda c: float(np.cov(c, b)[0, 1] / b.var()) if b.var() > 0 else float("nan")
        )
    results: list[dict[str, Any]] = []
    for m in body.methods:
        try:
            if m == "equal_weight":
                w = equal_weight(n)
            elif m == "min_variance":
                w = min_variance(cov, Bounds(0.0, cap))
            elif m == "mean_variance":
                w = mean_variance(mu, cov, body.risk_aversion, Bounds(0.0, cap))
            else:
                w = risk_parity(cov)
        except OptimizationError as exc:
            results.append({"method": m, "error": str(exc)})
            continue
        rc = risk_contributions(w, cov)
        weights = pd.Series(w, index=usable)
        results.append(
            {
                "method": m,
                "weights": {s: float(x) for s, x in weights.items()},
                "expected_volatility": float(math.sqrt(w @ cov @ w)),
                "historical_mean_return": float(w @ mu),
                "beta": float(weights @ betas.fillna(0)) if betas is not None else None,
                "risk_share": {s: float(x) for s, x in zip(usable, rc["pct"], strict=True)},
                "concentration": concentration(weights),
            }
        )
    return jsonable(
        {
            "as_of": str(window.index[-1].date()),
            "window_start": str(window.index[0].date()),
            "observations": len(window),
            "symbols": usable,
            "excluded_for_history": sorted(set(syms) - set(usable)),
            "covariance": body.covariance,
            "max_weight": cap,
            "is_synthetic": dv.is_synthetic,
            "results": results,
            "notes": [
                "Inputs use only returns dated on or before as_of.",
                "Expected volatility is in-sample; out-of-sample risk is usually higher.",
                "Mean-variance weights are sensitive to estimated means, which are noisy.",
            ],
        }
    )
