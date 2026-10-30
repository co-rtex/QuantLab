"""Out-of-sample validation studies built on the backtest engine.

Every study only ever re-runs the engine with different trading windows, parameters or
cost assumptions; the engine itself guarantees that each decision uses data available at
that time. Parameter selection inside walk-forward uses in-sample windows only.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from quantlab.analytics.performance import compute_performance, simple_returns
from quantlab.backtest.engine import BacktestConfig, BacktestError, run_backtest
from quantlab.data.adjust import PointInTimeAdjuster, total_returns
from quantlab.data.schema import wide
from quantlab.strategies.base import MarketView
from quantlab.strategies.library import create_strategy

Progress = Callable[[float, str], None]

# Small, interpretable default grids. Values outside each parameter's validated range are
# never produced because every combination is validated by the strategy's Pydantic model.
DEFAULT_GRIDS: dict[str, dict[str, list[Any]]] = {
    "momentum": {"lookback": [126, 252], "skip": [21], "top_n": [5, 10, 20]},
    "sma_crossover": {"fast": [20, 50], "slow": [100, 200]},
    "mean_reversion": {"lookback": [5, 10, 21], "bottom_n": [5, 10]},
    "low_volatility": {"lookback": [63, 126, 252], "top_n": [10, 20]},
    "min_variance": {"lookback": [63, 126, 252]},
    "risk_parity": {"lookback": [63, 126, 252]},
    "vol_target": {"target_vol": [0.06, 0.10, 0.14], "lookback": [21, 63]},
    "equal_weight": {},
    "buy_and_hold": {},
}


def param_grid(
    strategy_key: str, base: dict[str, Any], grid: dict[str, list[Any]] | None
) -> list[dict[str, Any]]:
    g = DEFAULT_GRIDS.get(strategy_key, {}) if grid is None else grid
    if not g:
        return [dict(base)]
    keys = sorted(g)
    combos = []
    for values in itertools.product(*(g[k] for k in keys)):
        params = {**base, **dict(zip(keys, values, strict=True))}
        try:
            create_strategy(strategy_key, params)
        except ValueError:
            continue  # invalid combination (e.g. fast >= slow)
        combos.append(params)
    if base and base not in combos:
        combos.append(dict(base))
    return combos


def _sharpe(res_equity: pd.Series, rf: float) -> float | None:
    v = compute_performance(res_equity, risk_free_rate=rf).values.get("sharpe")
    return None if v is None else float(v)


def _key(params: dict[str, Any]) -> str:
    return ", ".join(f"{k}={params[k]}" for k in sorted(params))


@dataclass
class Fold:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def make_folds(
    sessions: pd.DatetimeIndex,
    *,
    train_sessions: int = 504,
    test_sessions: int = 252,
    embargo: int = 5,
    anchored: bool = False,
) -> list[Fold]:
    folds = []
    start = 0
    while True:
        tr0 = 0 if anchored else start
        tr1 = start + train_sessions - 1
        te0 = tr1 + 1 + embargo
        te1 = min(te0 + test_sessions - 1, len(sessions) - 1)
        if te0 >= len(sessions) - 20:
            break
        folds.append(Fold(sessions[tr0], sessions[tr1], sessions[te0], sessions[te1]))
        start += test_sessions
    return folds


def walk_forward(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    strategy_key: str,
    base_params: dict[str, Any],
    config: BacktestConfig,
    *,
    grid: dict[str, list[Any]] | None = None,
    train_sessions: int = 504,
    test_sessions: int = 252,
    embargo: int = 5,
    anchored: bool = False,
    on_progress: Progress | None = None,
) -> dict[str, Any]:
    """Rolling (or anchored) walk-forward: choose parameters by in-sample Sharpe, then
    evaluate them on the following, unseen window. OOS segments are stitched together."""
    all_sessions = pd.DatetimeIndex(sorted(bars["date"].unique()))
    sessions = (
        all_sessions[all_sessions >= pd.Timestamp(config.start)] if config.start else all_sessions
    )
    if config.end:
        sessions = sessions[sessions <= pd.Timestamp(config.end)]
    folds = make_folds(
        sessions,
        train_sessions=train_sessions,
        test_sessions=test_sessions,
        embargo=embargo,
        anchored=anchored,
    )
    if not folds:
        return {
            "error": "sample too short for the requested walk-forward windows",
            "sessions": len(sessions),
        }
    combos = param_grid(strategy_key, base_params, grid)
    total = len(folds) * (len(combos) + 1)
    done = 0
    rows: list[dict[str, Any]] = []
    oos_returns: list[pd.Series] = []
    for i, f in enumerate(folds):
        best: tuple[float, dict[str, Any]] | None = None
        is_scores = []
        for params in combos:
            cfg = config.model_copy(
                update={"start": f.train_start.date(), "end": f.train_end.date()}
            )
            try:
                res = run_backtest(
                    bars,
                    actions,
                    create_strategy(strategy_key, params),
                    cfg,
                    record_positions=False,
                )
                s = _sharpe(res.equity["equity"], config.risk_free_rate)
            except BacktestError:
                s = None
            is_scores.append({"params": _key(params), "sharpe": s})
            score = -math.inf if s is None else s
            if best is None or score > best[0]:
                best = (score, params)
            done += 1
            if on_progress:
                on_progress(done / total, f"fold {i + 1}/{len(folds)}: in-sample search")
        assert best is not None
        cfg = config.model_copy(update={"start": f.test_start.date(), "end": f.test_end.date()})
        res = run_backtest(
            bars, actions, create_strategy(strategy_key, best[1]), cfg, record_positions=False
        )
        oos = compute_performance(
            res.equity["equity"], res.benchmark, risk_free_rate=config.risk_free_rate
        )
        oos_returns.append(simple_returns(res.equity["equity"]))
        rows.append(
            {
                "fold": i + 1,
                "train": [str(f.train_start.date()), str(f.train_end.date())],
                "test": [str(f.test_start.date()), str(f.test_end.date())],
                "selected_params": best[1],
                "in_sample_sharpe": None if best[0] == -math.inf else best[0],
                "oos_sharpe": oos.values.get("sharpe"),
                "oos_return": oos.values.get("total_return"),
                "oos_benchmark_return": oos.values.get("benchmark_total_return"),
                "oos_max_drawdown": oos.values.get("max_drawdown"),
                "in_sample_grid": is_scores,
            }
        )
        done += 1
        if on_progress:
            on_progress(done / total, f"fold {i + 1}/{len(folds)}: out-of-sample test")
    stitched = pd.concat(oos_returns).sort_index()
    stitched = stitched[~stitched.index.duplicated()]
    oos_equity = config.initial_capital * (1 + stitched).cumprod()
    combined = compute_performance(
        pd.concat(
            [
                pd.Series(
                    [config.initial_capital], index=[stitched.index[0] - pd.Timedelta(days=1)]
                ),
                oos_equity,
            ]
        ),
        risk_free_rate=config.risk_free_rate,
    )
    is_sharpes = [r["in_sample_sharpe"] for r in rows if r["in_sample_sharpe"] is not None]
    oos_sharpes = [r["oos_sharpe"] for r in rows if r["oos_sharpe"] is not None]
    chosen = [_key(r["selected_params"]) for r in rows]
    return {
        "method": "anchored" if anchored else "rolling",
        "train_sessions": train_sessions,
        "test_sessions": test_sessions,
        "embargo": embargo,
        "grid_size": len(combos),
        "folds": rows,
        "oos_combined": combined.to_dict(),
        "oos_equity": {
            "dates": [d.strftime("%Y-%m-%d") for d in oos_equity.index],
            "equity": [float(v) for v in oos_equity.to_numpy()],
        },
        "mean_in_sample_sharpe": float(np.mean(is_sharpes)) if is_sharpes else None,
        "mean_oos_sharpe": float(np.mean(oos_sharpes)) if oos_sharpes else None,
        "param_changes": sum(a != b for a, b in itertools.pairwise(chosen)),
        "distinct_params_selected": len(set(chosen)),
        "oos_returns": stitched,  # removed before serialization by the caller
    }


def sensitivity(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    strategy_key: str,
    base_params: dict[str, Any],
    config: BacktestConfig,
    *,
    grid: dict[str, list[Any]] | None = None,
    on_progress: Progress | None = None,
) -> dict[str, Any]:
    """Full-sample metrics across the parameter grid (in-sample by construction)."""
    combos = param_grid(strategy_key, base_params, grid)
    rows: list[dict[str, Any]] = []
    per_period: list[float] = []
    for i, params in enumerate(combos):
        res = run_backtest(
            bars, actions, create_strategy(strategy_key, params), config, record_positions=False
        )
        m = compute_performance(
            res.equity["equity"], res.benchmark, risk_free_rate=config.risk_free_rate
        ).values
        r = simple_returns(res.equity["equity"])
        if r.std(ddof=1) > 0:
            per_period.append(float(r.mean() / r.std(ddof=1)))
        rows.append(
            {
                "params": params,
                "label": _key(params),
                "is_base": params == base_params,
                "sharpe": m.get("sharpe"),
                "cagr": m.get("cagr"),
                "max_drawdown": m.get("max_drawdown"),
                "volatility": m.get("volatility"),
            }
        )
        if on_progress:
            on_progress((i + 1) / len(combos), f"sensitivity {i + 1}/{len(combos)}")
    sharpes = [r["sharpe"] for r in rows if r["sharpe"] is not None]
    base = next((r for r in rows if r["is_base"]), None)
    summary: dict[str, Any] = {
        "grid_size": len(rows),
        "positive_sharpe_fraction": (sum(s > 0 for s in sharpes) / len(sharpes))
        if sharpes
        else None,
        "sharpe_range": [min(sharpes), max(sharpes)] if sharpes else None,
        "sharpe_std": float(np.std(sharpes, ddof=1)) if len(sharpes) > 1 else None,
        "base_rank": (sorted(sharpes, reverse=True).index(base["sharpe"]) + 1)
        if base and base["sharpe"] is not None
        else None,
    }
    return {"rows": rows, "summary": summary, "trial_sharpes_per_period": per_period}


def cost_sensitivity(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    strategy_key: str,
    params: dict[str, Any],
    config: BacktestConfig,
    *,
    multipliers: tuple[float, ...] = (0.0, 0.5, 1.0, 2.0, 4.0, 8.0),
    on_progress: Progress | None = None,
) -> dict[str, Any]:
    """Net performance as trading costs scale; reports where excess return turns negative."""
    rows: list[dict[str, Any]] = []
    base_costs = config.commission_bps + config.slippage_bps
    for i, k in enumerate(multipliers):
        cfg = config.with_costs(k)
        res = run_backtest(
            bars, actions, create_strategy(strategy_key, params), cfg, record_positions=False
        )
        m = compute_performance(
            res.equity["equity"], res.benchmark, risk_free_rate=config.risk_free_rate
        ).values
        rows.append(
            {
                "multiplier": k,
                "round_trip_bps": 2 * base_costs * k,
                "sharpe": m.get("sharpe"),
                "cagr": m.get("cagr"),
                "excess_total_return": m.get("excess_total_return"),
            }
        )
        if on_progress:
            on_progress((i + 1) / len(multipliers), f"cost sweep {i + 1}/{len(multipliers)}")
    breakeven = None
    for a, b in itertools.pairwise(rows):
        ea, eb = a["excess_total_return"], b["excess_total_return"]
        if ea is not None and eb is not None and ea > 0 >= eb:
            t = ea / (ea - eb)
            breakeven = a["multiplier"] + t * (b["multiplier"] - a["multiplier"])
            break
    zero = rows[0]["excess_total_return"] if rows else None
    note = None
    if breakeven is None and zero is not None:
        note = (
            "underperforms the benchmark even with zero costs"
            if zero <= 0
            else "beats the benchmark at every tested cost level"
        )
    return {
        "rows": rows,
        "breakeven_multiplier": breakeven,
        "note": note,
        "base_costs_bps": {"commission": config.commission_bps, "slippage": config.slippage_bps},
    }


def leakage_check(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    strategy_key: str,
    params: dict[str, Any],
    config: BacktestConfig,
    *,
    samples: int = 8,
    seed: int = 0,
) -> dict[str, Any]:
    """Empirical look-ahead test.

    For sampled decision dates ``d``, compute target weights twice: from a view built on the
    full dataset and from a view built on data truncated at ``d``. Any difference means the
    signal depends on information dated after ``d``.
    """
    universe = sorted(
        set(config.universe or bars["symbol"].unique())
        - ({config.benchmark.upper()} if config.benchmark else set())
    )
    b = bars[bars["symbol"].isin(universe)]
    a = actions[actions["symbol"].isin(universe)]
    dates = pd.DatetimeIndex(sorted(b["date"].unique()))
    strat = create_strategy(strategy_key, params)
    lo = max(strat.warmup + 1, 1)
    if config.start:
        lo = max(lo, int(dates.searchsorted(pd.Timestamp(config.start))))
    candidates = np.arange(lo, len(dates) - 1)
    if len(candidates) == 0:
        return {"status": "unassessed", "reason": "no eligible decision dates"}
    rng = np.random.default_rng(seed)
    picks = sorted(rng.choice(candidates, size=min(samples, len(candidates)), replace=False))

    def view(bars_: pd.DataFrame, actions_: pd.DataFrame, d: pd.Timestamp) -> MarketView:
        close = wide(bars_, "close").reindex(columns=universe)
        k = int(close.index.get_indexer([d])[0])
        adj = PointInTimeAdjuster(close, actions_)
        adj_close = close.to_numpy() * adj.multiplier(k)
        rets = total_returns(bars_, actions_).reindex(index=close.index, columns=universe)
        return MarketView(
            date=d,
            close=pd.DataFrame(adj_close[: k + 1], index=close.index[: k + 1], columns=universe),
            returns=rets.iloc[: k + 1],
            volume=wide(bars_, "volume").reindex(index=close.index, columns=universe).iloc[: k + 1],
            tradable=pd.Series(np.isfinite(close.to_numpy()[k]), index=universe),
            holdings=pd.Series(0.0, index=universe),
        )

    mismatches = []
    for k in picks:
        d = dates[k]
        w_full = strat.target_weights(view(b, a, d))
        tb, ta = b[b["date"] <= d], a[a["ex_date"] <= d]
        w_trunc = strat.target_weights(view(tb, ta, d))
        s1 = pd.Series(dtype=float) if w_full is None else w_full.sort_index()
        s2 = pd.Series(dtype=float) if w_trunc is None else w_trunc.sort_index()
        same = s1.index.equals(s2.index) and np.allclose(s1.to_numpy(), s2.to_numpy(), atol=1e-10)
        if not same:
            mismatches.append(str(d.date()))
    return {
        "status": "verified" if not mismatches else "failed",
        "dates_tested": [str(dates[k].date()) for k in picks],
        "mismatches": mismatches,
        "method": "weights from full data vs data truncated at each decision date",
    }
