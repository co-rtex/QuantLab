"""Reproducible cross-sectional ML experiment.

Problem: at each sample date ``t`` predict, for every tradable asset, whether its total
return over the next ``horizon`` sessions will beat the cross-sectional median
(``label = 1``). This is a ranking problem; a model is useful only if it orders assets
better than simple baselines.

Timestamp integrity:

* features at ``t`` use only bars dated ``<= t`` (verified by a truncation test),
* the label at ``t`` uses closes from ``t`` to ``t + horizon``; it becomes known at
  ``t + horizon``,
* cross-validation is chronological; training rows whose label window overlaps the test
  window are purged and an embargo of ``horizon`` sessions separates train and test.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator
from scipy import stats
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from quantlab.data.adjust import PointInTimeAdjuster, total_returns
from quantlab.data.schema import wide

FEATURES = [
    "mom_21",
    "mom_63",
    "mom_126",
    "mom_252_21",
    "rev_5",
    "vol_21",
    "vol_63",
    "dist_high_252",
    "volume_z_21",
]
ModelName = Literal["majority", "momentum_rule", "logistic", "random_forest", "gradient_boosting"]
MODELS: tuple[ModelName, ...] = (
    "majority",
    "momentum_rule",
    "logistic",
    "random_forest",
    "gradient_boosting",
)


class MLConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    horizon: int = Field(21, ge=5, le=126)
    sample_every: int = Field(21, ge=1, le=63)
    folds: int = Field(4, ge=2, le=10)
    min_train_dates: int = Field(24, ge=6, le=500)
    models: tuple[ModelName, ...] = Field(MODELS, min_length=1, max_length=len(MODELS))
    seed: int = Field(0, ge=0, le=2**31 - 1)
    exclude: tuple[str, ...] = Field(("SYNIDX",), max_length=50)

    @field_validator("models")
    @classmethod
    def _unique(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(v)) != len(v):
            raise ValueError("models must not repeat")
        return v


def compute_features(
    bars: pd.DataFrame, actions: pd.DataFrame, exclude: tuple[str, ...] = ("SYNIDX",)
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Date x symbol feature matrices plus fully adjusted closes.

    Fully adjusted levels are safe here because only *ratios* of prices dated on or before
    ``t`` are used: corporate actions after ``t`` scale both sides equally and cancel.
    """
    bars = bars[~bars["symbol"].isin(exclude)]
    close = wide(bars, "close")
    vol = wide(bars, "volume").reindex(index=close.index, columns=close.columns)
    acts = actions[actions["symbol"].isin(close.columns)]
    adj = close * PointInTimeAdjuster(close, acts).multiplier(None)
    rets = total_returns(bars, acts).reindex(index=close.index, columns=close.columns)

    def mom(n: int, skip: int = 0) -> pd.DataFrame:
        return adj.shift(skip) / adj.shift(n) - 1

    logv = np.log1p(vol)
    feats = {
        "mom_21": mom(21),
        "mom_63": mom(63),
        "mom_126": mom(126),
        "mom_252_21": mom(252, 21),
        "rev_5": mom(5),
        "vol_21": rets.rolling(21).std(),
        "vol_63": rets.rolling(63).std(),
        "dist_high_252": adj / adj.rolling(252).max() - 1,
        "volume_z_21": (logv - logv.rolling(21).mean()) / logv.rolling(21).std(),
    }
    return feats, adj


def build_panel(bars: pd.DataFrame, actions: pd.DataFrame, cfg: MLConfig) -> pd.DataFrame:
    """Long panel (date, symbol, features..., fwd_return, label, label_available_at)."""
    feats, adj = compute_features(bars, actions, cfg.exclude)
    close = adj
    h = cfg.horizon
    # The label runs to the last close at or before t+h. For an asset that stops trading
    # inside the horizon that is the return to delisting (the engine liquidates at the last
    # close). Using adj.shift(-h) alone made the label NaN and the row was dropped, so the
    # panel only contained assets that survived to t+h: selection on future information.
    fwd = adj.ffill().shift(-h) / adj - 1
    dates = close.index[252 :: cfg.sample_every]
    dates = dates[dates <= close.index[-1 - h]] if len(close) > h else dates[:0]
    frames = []
    for d in dates:
        k = close.index.get_loc(d)
        row = pd.DataFrame({name: f.loc[d] for name, f in feats.items()})
        row["fwd_return"] = fwd.loc[d]
        row["tradable"] = close.loc[d].notna()
        row = row[row["tradable"]].drop(columns="tradable").dropna()
        if len(row) < 10:
            continue
        row["label"] = (row["fwd_return"] > row["fwd_return"].median()).astype(int)
        row["date"] = d
        row["label_available_at"] = close.index[k + h]
        frames.append(row.rename_axis("symbol").reset_index())
    if not frames:
        return pd.DataFrame(
            columns=["date", "symbol", *FEATURES, "fwd_return", "label", "label_available_at"]
        )
    panel = pd.concat(frames, ignore_index=True)
    return panel[["date", "symbol", *FEATURES, "fwd_return", "label", "label_available_at"]]


def panel_hash(panel: pd.DataFrame) -> str:
    return hashlib.sha256(
        pd.util.hash_pandas_object(panel, index=False).to_numpy().tobytes()
    ).hexdigest()


@dataclass
class Split:
    fold: int
    train_dates: pd.DatetimeIndex
    test_dates: pd.DatetimeIndex


def purged_splits(panel: pd.DataFrame, cfg: MLConfig) -> list[Split]:
    """Expanding-window chronological splits with purge and embargo.

    A training row is kept only if its label became available strictly before the first
    test date minus an embargo of ``horizon`` sessions (approximated in sample dates).
    """
    dates = pd.DatetimeIndex(sorted(panel["date"].unique()))
    embargo_steps = max(1, math.ceil(cfg.horizon / cfg.sample_every))
    # The first test window starts after min_train_dates *plus* the embargo, so the first
    # fold still trains on min_train_dates once the embargo is removed.
    first = cfg.min_train_dates + embargo_steps
    usable = len(dates) - first
    if usable < cfg.folds:
        return []
    size = usable // cfg.folds
    avail = panel.groupby("date")["label_available_at"].max()
    out = []
    for i in range(cfg.folds):
        t0 = first + i * size
        t1 = len(dates) if i == cfg.folds - 1 else t0 + size
        test = dates[t0:t1]
        cutoff_idx = max(0, t0 - embargo_steps)
        train = dates[:cutoff_idx]
        train = train[avail.reindex(train).to_numpy() < test[0]]
        out.append(Split(i + 1, pd.DatetimeIndex(train), test))
    # min_train_dates is a floor on what the model actually trains on, i.e. after purge and
    # embargo. A long horizon could otherwise leave a fold with no training rows at all and
    # the run failed deep inside scikit-learn ("Input contains NaN").
    if any(len(s.train_dates) < cfg.min_train_dates for s in out):
        return []
    return out


def _model(name: str, seed: int) -> Any:
    if name == "logistic":
        return make_pipeline(StandardScaler(), LogisticRegression(C=0.1, max_iter=1000))
    if name == "random_forest":
        return RandomForestClassifier(
            n_estimators=200, max_depth=4, min_samples_leaf=50, n_jobs=1, random_state=seed
        )
    if name == "gradient_boosting":
        return HistGradientBoostingClassifier(
            max_depth=3, learning_rate=0.05, max_iter=150, l2_regularization=1.0, random_state=seed
        )
    raise ValueError(name)


def _cross_sectional_rank(panel: pd.DataFrame, col: str) -> np.ndarray:
    return panel.groupby("date")[col].rank(pct=True).to_numpy()


def _metrics(test: pd.DataFrame, prob: np.ndarray) -> dict[str, Any]:
    y = test["label"].to_numpy()
    p = np.clip(prob, 1e-6, 1 - 1e-6)
    out: dict[str, Any] = {
        "rows": len(y),
        "accuracy": float(((p > 0.5).astype(int) == y).mean()),
        "brier": float(brier_score_loss(y, p)),
        "log_loss": float(log_loss(y, p, labels=[0, 1])),
        "auc": float(roc_auc_score(y, p)) if len(set(y)) == 2 and np.ptp(p) > 0 else None,
    }
    frame = test.assign(score=prob)
    ics, spreads = [], []
    for _, g in frame.groupby("date"):
        if g["score"].nunique() > 1 and len(g) >= 10:
            ics.append(stats.spearmanr(g["score"], g["fwd_return"]).statistic)
            q = g["score"].quantile([0.2, 0.8])
            top = g.loc[g["score"] >= q.iloc[1], "fwd_return"].mean()
            bottom = g.loc[g["score"] <= q.iloc[0], "fwd_return"].mean()
            spreads.append(top - bottom)
    ic = np.asarray([x for x in ics if np.isfinite(x)])
    out["ic_mean"] = float(ic.mean()) if len(ic) else None
    out["ic_t_stat"] = (
        float(ic.mean() / (ic.std(ddof=1) / math.sqrt(len(ic))))
        if len(ic) > 2 and ic.std(ddof=1) > 0
        else None
    )
    out["top_minus_bottom_quintile"] = float(np.mean(spreads)) if spreads else None
    out["periods"] = len(ic)
    return out


def run_ml_experiment(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    cfg: MLConfig | None = None,
    on_progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    cfg = cfg or MLConfig()
    unknown = set(cfg.models) - set(MODELS)
    if unknown:
        raise ValueError(f"unknown models: {sorted(unknown)}")
    panel = build_panel(bars, actions, cfg)
    splits = purged_splits(panel, cfg)
    if not splits:
        raise ValueError(
            "not enough sample dates for the requested folds: each fold needs "
            f"min_train_dates={cfg.min_train_dates} training dates after a purge and embargo of "
            f"horizon={cfg.horizon} sessions; use fewer folds, a shorter horizon or more "
            "history"
        )
    preds = []
    fold_rows = []
    importances: dict[str, list[np.ndarray]] = {}
    steps = len(splits) * len(cfg.models)
    done = 0
    for sp in splits:
        train = panel[panel["date"].isin(sp.train_dates)]
        test = panel[panel["date"].isin(sp.test_dates)]
        X, y, Xt = train[FEATURES].to_numpy(), train["label"].to_numpy(), test[FEATURES].to_numpy()
        for name in cfg.models:
            if name == "majority":
                prob = np.full(len(test), y.mean())
            elif name == "momentum_rule":
                prob = _cross_sectional_rank(test, "mom_252_21")
            else:
                model = _model(name, cfg.seed).fit(X, y)
                prob = model.predict_proba(Xt)[:, 1]
                if name == "logistic":
                    coef = model[-1].coef_[0]
                    importances.setdefault(name, []).append(np.abs(coef) / np.abs(coef).sum())
                elif name == "random_forest":
                    importances.setdefault(name, []).append(model.feature_importances_)
            m = _metrics(test, prob)
            fold_rows.append(
                {
                    "fold": sp.fold,
                    "model": name,
                    "train_dates": len(sp.train_dates),
                    "train_rows": len(train),
                    "test_start": str(sp.test_dates[0].date()),
                    "test_end": str(sp.test_dates[-1].date()),
                    **m,
                }
            )
            preds.append(
                test[["date", "symbol", "fwd_return", "label"]].assign(
                    model=name, fold=sp.fold, prob=prob
                )
            )
            done += 1
            if on_progress:
                on_progress(done / steps, f"fold {sp.fold}: {name}")
    all_preds = pd.concat(preds, ignore_index=True)
    summary = []
    for name in cfg.models:
        g = all_preds[all_preds["model"] == name]
        m = _metrics(g, g["prob"].to_numpy())
        summary.append({"model": name, **m})
    base_auc = next((s["auc"] for s in summary if s["model"] == "momentum_rule"), None)
    return {
        "problem": f"P(next {cfg.horizon}-session total return beats the cross-sectional median)",
        "config": cfg.model_dump(mode="json"),
        "features": FEATURES,
        "panel": {
            "rows": len(panel),
            "dates": int(panel["date"].nunique()),
            "symbols": int(panel["symbol"].nunique()),
            "hash": panel_hash(panel),
            "positive_rate": float(panel["label"].mean()),
        },
        "splits": [
            {
                "fold": s.fold,
                "train_dates": len(s.train_dates),
                "test_start": str(s.test_dates[0].date()),
                "test_end": str(s.test_dates[-1].date()),
            }
            for s in splits
        ],
        "summary": summary,
        "folds": fold_rows,
        "feature_importance": {
            k: dict(zip(FEATURES, np.mean(v, axis=0).round(4).tolist(), strict=True))
            for k, v in importances.items()
        },
        "baseline_auc": base_auc,
        "predictions": all_preds,  # stored as an artifact by the caller, not serialized
        "limitations": [
            "Out-of-sample metrics are pooled across expanding-window folds on one dataset.",
            "Cross-sectional labels ignore trading costs and capacity.",
            "AUC near 0.5 means no ranking skill; small differences between models may be "
            "noise (see the IC t-statistic).",
        ],
    }
