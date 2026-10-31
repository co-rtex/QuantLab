"""Reproducible cross-sectional ML experiment.

Problem: at each sample date ``t`` predict, for every tradable asset, whether its total
return over the next ``horizon`` sessions will beat the cross-sectional median
(``label = 1``). This is a ranking problem; a model is useful only if it orders assets
better than simple baselines.

Timestamp integrity:

* features at ``t`` use only bars dated ``<= t`` (verified by a truncation test). Windows
  count the instrument's own observed bars, so a halt shortens nothing and voids nothing:
  a 21-bar window just spans more calendar sessions across it (#120). A bar with zero
  volume (a stale close nobody traded at) is no bar: it is removed before anything else,
  so it is neither a decision row nor a price a label starts or ends at (#142),
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
from quantlab.data.identity import normalize_master
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
    # 1 where the 21-bar volume was constant and non-zero, so volume_z_21 = 0 there means
    # "no dispersion", not "average volume" (#125 review).
    "volume_flat_21",
    # Staleness covariates (#120): trade-time windows can reach back further in calendar
    # time, after a halt or for a thinly traded name. Instead of excluding such rows (a
    # selection against illiquid names), the models see how stale the windows are.
    # (s[t] - s[t-21]) / 21 over the instrument's bars, s = session position: the calendar
    # span of mom_21's 21 returns per return (1 = a bar every session, 2 = every other
    # session). Zero-volume bars are removed first, so they count as sessions without a bar.
    "bars_span_21",
    # log1p of max_gap_252 (the raw count is unbounded; the panel keeps it as max_gap_252).
    "log1p_max_gap_252",
]
PANEL_COLUMNS = [
    "date",
    "symbol",
    *FEATURES,
    # Sessions without a bar in the longest gap between consecutive bars over the last 252
    # returns (bars t-252..t); a zero-volume bar is no bar. Reported, not a model input
    # (see log1p_max_gap_252).
    "max_gap_252",
    "fwd_return",
    "label",
    "label_available_at",
]
# Bumped whenever the panel a given dataset and config produce can change, so that an
# old run reproduced on new code shows a named difference instead of a silent one.
#   1: rolling windows over calendar sessions
#   2: windows over each instrument's own bars (#120); flat non-zero volume gives
#      volume_z_21 = 0 with the volume_flat_21 input; staleness inputs bars_span_21 and
#      log1p_max_gap_252; rows with no volume in 21 bars are excluded (untraded_21)
#   3: a zero-volume bar is no bar (#142): removed before the features, so it is not a
#      decision row, label price or window member and counts as a gap; reported as
#      zero_volume_bars. That made the untraded_21 exclusion unreachable, so it is gone
ML_PIPELINE_VERSION = 3
# Below this, a rolling dispersion counts as zero (as in quantlab.analytics).
_ZERO_DISPERSION = 1e-12
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
    # The pipeline version the run was submitted for, stamped by the API so the worker can
    # refuse to compute it with different code (#143). None: not stamped (older runs).
    pipeline_version: int | None = Field(None, ge=1)

    @field_validator("models")
    @classmethod
    def _unique(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(v)) != len(v):
            raise ValueError("models must not repeat")
        return v


def _own_bar_features(
    adj: pd.DataFrame,
    ret: pd.DataFrame,
    vol: pd.DataFrame,
    session: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    """Features over rows that are each instrument's consecutive *own* bars.

    Every window and lag is counted in bars (trade time), so a value uses its own bar and
    earlier ones, never a later one. ``session`` holds each bar's position on the session
    calendar, which gives the staleness covariates. Zero-volume bars were removed before
    (``_features``), so every volume here is positive or unknown (NaN).
    """

    def mom(n: int, skip: int = 0) -> pd.DataFrame:
        return adj.shift(skip) / adj.shift(n) - 1

    logv = np.log1p(vol)
    v_mean, v_std = logv.rolling(21).mean(), logv.rolling(21).std()
    # Constant volume deviates from its mean by exactly zero, so its z-score is 0, not
    # 0/0, and the flag keeps the case visible. An unknown volume leaves the 21 windows
    # that contain it undefined.
    flat = v_std <= _ZERO_DISPERSION
    z = ((logv - v_mean) / v_std.where(~flat)).where(~flat, 0.0)
    z = z.where(v_std.notna())
    feats = {
        "mom_21": mom(21),
        "mom_63": mom(63),
        "mom_126": mom(126),
        "mom_252_21": mom(252, 21),
        "rev_5": mom(5),
        "vol_21": ret.rolling(21).std(),
        "vol_63": ret.rolling(63).std(),
        "dist_high_252": adj / adj.rolling(252).max() - 1,
        "volume_z_21": z,
    }
    # Staleness is described, not punished: a halt is one long gap (large max_gap_252),
    # thin trading many short ones (large bars_span_21, small max_gap_252). A bar with an
    # unknown volume still has a close, so it counts as a bar here as everywhere else.
    feats["bars_span_21"] = (session - session.shift(21)) / 21
    gap = session - session.shift(1) - 1  # sessions without a bar just before this bar
    feats["max_gap_252"] = gap.rolling(252).max()
    feats["log1p_max_gap_252"] = np.log1p(feats["max_gap_252"])
    feats["volume_flat_21"] = flat.astype(float).where(v_std.notna())
    return feats


def compute_features(
    bars: pd.DataFrame, actions: pd.DataFrame, exclude: tuple[str, ...] = ("SYNIDX",)
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Date x symbol feature matrices plus fully adjusted closes (see ``_features``)."""
    feats, _, adj = _features(bars, actions, exclude)
    return feats, adj


def _features(
    bars: pd.DataFrame, actions: pd.DataFrame, exclude: tuple[str, ...]
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, pd.DataFrame]:
    """Features, the removed zero-volume bars and fully adjusted closes, date x symbol.

    A bar with zero volume (a stale close a vendor keeps printing through a halt, or for a
    name that rarely trades) is no bar (#142). It is removed before anything is computed,
    so it reads exactly like a session without a bar: it is not a decision row or a label
    price, windows skip it, the staleness inputs count it as a gap, and a corporate action
    dated on it applies on the next bar, as it would across a gap. A bar with an unknown
    (NaN) volume still has a close and is kept. The session calendar is that of all bars.

    Fully adjusted levels are safe here because only *ratios* of prices dated on or before
    ``t`` are used: corporate actions after ``t`` scale both sides equally and cancel.

    Features are computed over each instrument's own bars and placed back on the session
    calendar, NaN where it has no bar. A halt therefore removes a security only for the
    sessions it has no bar on (#120). Before, any window that contained a halt day was NaN
    for the window's length (up to 252 sessions). For speed, each column's bars are moved
    to consecutive rows (a "compressed" matrix), the usual vectorized windows run once, and
    the values are scattered back.
    """
    bars = bars[~bars["symbol"].isin(exclude)]
    calendar = wide(bars, "close")
    zero = bars["volume"] <= 0  # negative volume is a quality error; no trade either way
    zero_volume = (
        wide(bars.assign(zero=zero.astype(float)), "zero")
        .reindex(index=calendar.index, columns=calendar.columns)
        .eq(1)
    )
    bars = bars[~zero]
    close = wide(bars, "close").reindex(index=calendar.index, columns=calendar.columns)
    vol = wide(bars, "volume").reindex(index=close.index, columns=close.columns)
    acts = actions[actions["symbol"].isin(close.columns)]
    adj = close * PointInTimeAdjuster(close, acts).multiplier(None)
    # The first bar after a gap carries the return over the whole gap (total_returns).
    rets = total_returns(bars, acts).reindex(index=close.index, columns=close.columns)
    has = close.notna().to_numpy()
    rows = np.cumsum(has, axis=0) - 1  # each bar's position among its instrument's bars
    r_i, c_i = rows[has], np.nonzero(has)[1]

    def compress(frame: pd.DataFrame) -> pd.DataFrame:
        out = np.full(frame.shape, np.nan)
        out[r_i, c_i] = frame.to_numpy(dtype=float)[has]
        return pd.DataFrame(out, columns=frame.columns)

    def expand(frame: pd.DataFrame) -> pd.DataFrame:
        out = np.full(frame.shape, np.nan)
        out[has] = frame.to_numpy(dtype=float)[r_i, c_i]
        return pd.DataFrame(out, index=close.index, columns=close.columns)

    session = pd.DataFrame(
        np.broadcast_to(np.arange(len(close), dtype=float)[:, None], close.shape),
        index=close.index,
        columns=close.columns,
    )
    own = _own_bar_features(compress(adj), compress(rets), compress(vol), compress(session))
    return {name: expand(f) for name, f in own.items()}, zero_volume, adj


def build_panel(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    cfg: MLConfig,
    *,
    instruments: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Long panel (date, symbol, features..., max_gap_252, fwd_return, label,
    label_available_at). The features include ``volume_flat_21`` (1.0 where the 21-bar
    volume was constant and non-zero, so ``volume_z_21`` is 0) and the staleness inputs;
    ``max_gap_252`` is the raw gap count behind ``log1p_max_gap_252``, reported only.

    ``instruments`` is an instrument master keyed like ``bars["symbol"]``; its delisting
    returns are applied to labels whose horizon reaches the first session after the
    security's delisting date.
    """
    feats, zero_volume, adj = _features(bars, actions, cfg.exclude)
    close = adj
    h = cfg.horizon
    # The label runs to the last close at or before t+h. For an asset that stops trading
    # inside the horizon that is the return to delisting (the engine liquidates at the last
    # close). Using adj.shift(-h) alone made the label NaN and the row was dropped, so the
    # panel only contained assets that survived to t+h: selection on future information.
    # With an instrument master, a security exits at its delisting exactly as in the
    # engine: its exit price is the last close on or before the delisting date (bars after
    # it, an OTC tail say, are ignored and it is not sampled after it), and a known
    # delisting return applies only to labels whose horizon reaches the first session
    # after the delisting date, the moment the engine books it. So no label includes a
    # return that is not public by its label_available_at. Without a return the
    # optimistic last-close exit remains (documented in METHODOLOGY).
    exit_px = adj
    listed = pd.DataFrame(True, index=adj.index, columns=adj.columns)
    booked: list[tuple[str, int, float]] = []
    if instruments is not None and len(instruments):
        master = normalize_master(instruments)
        master = master[
            master["delisting_date"].notna() & master["instrument_id"].isin(adj.columns)
        ]
        if len(master):
            exit_px = adj.copy()
            for row in master.itertuples():
                when = pd.Timestamp(row.delisting_date)
                exit_px.loc[exit_px.index > when, row.instrument_id] = np.nan
                listed.loc[listed.index > when, row.instrument_id] = False
                if pd.notna(row.delisting_return):
                    first_after = int(adj.index.searchsorted(when, side="right"))
                    booked.append((row.instrument_id, first_after, float(row.delisting_return)))
    fwd = exit_px.ffill().shift(-h) / adj - 1
    positions = np.arange(len(adj))
    for sym, first_after, r in booked:
        reaches = positions + h >= first_after
        fwd.loc[reaches, sym] = (1 + fwd.loc[reaches, sym]) * (1 + r) - 1
    close = exit_px
    dates = close.index[252 :: cfg.sample_every]
    dates = dates[dates <= close.index[-1 - h]] if len(close) > h else dates[:0]
    frames = []
    # Zero-volume bars of listed names on a sampled date: not decision rows (#142).
    stale = zero_volume & listed
    excluded = {"zero_volume_bars": 0, "dates_below_10_with_zero_volume": 0}
    for d in dates:
        k = close.index.get_loc(d)
        row = pd.DataFrame({name: f.loc[d] for name, f in feats.items()})  # incl. the flag
        row["fwd_return"] = fwd.loc[d]
        row = row[close.loc[d].notna()].dropna()
        n_stale = int(stale.loc[d].sum())
        if len(row) < 10:
            # A date too thin for a cross-section is skipped whole; say when it had enough
            # names counting its zero-volume bars (an upper bound: such a bar's features
            # could have been incomplete anyway).
            if len(row) + n_stale >= 10:
                excluded["dates_below_10_with_zero_volume"] += 1
            continue
        excluded["zero_volume_bars"] += n_stale  # only on dates that make the panel
        row["label"] = (row["fwd_return"] > row["fwd_return"].median()).astype(int)
        row["date"] = d
        row["label_available_at"] = close.index[k + h]
        frames.append(row.rename_axis("symbol").reset_index())
    if not frames:
        empty = pd.DataFrame(columns=PANEL_COLUMNS)
        empty.attrs["excluded"] = excluded
        return empty
    panel = pd.concat(frames, ignore_index=True)[PANEL_COLUMNS]
    # Rows left out for a stated reason (not warm-up): reported with the results.
    panel.attrs["excluded"] = excluded
    return panel


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
    *,
    instruments: pd.DataFrame | None = None,
) -> dict[str, Any]:
    cfg = cfg or MLConfig()
    if cfg.pipeline_version is not None and cfg.pipeline_version != ML_PIPELINE_VERSION:
        # A mixed-version deploy: the input hash names the submitted version, so computing
        # it with other code would make the stored hash describe a different panel.
        raise ValueError(
            f"this ML run was submitted for pipeline version {cfg.pipeline_version}, but this "
            f"worker runs version {ML_PIPELINE_VERSION}; submit it again so its input hash "
            "matches the code that computes it"
        )
    unknown = set(cfg.models) - set(MODELS)
    if unknown:
        raise ValueError(f"unknown models: {sorted(unknown)}")
    panel = build_panel(bars, actions, cfg, instruments=instruments)
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
        "pipeline_version": ML_PIPELINE_VERSION,
        "features": FEATURES,
        "panel": {
            "rows": len(panel),
            "volume_flat_rows": int(panel["volume_flat_21"].sum()),
            # Rows left out for a stated reason, among those otherwise kept: no volume at
            # all in the last 21 bars. Stale windows are not excluded; see bars_span_21
            # and max_gap_252.
            "excluded_rows": dict(panel.attrs.get("excluded", {})),
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
