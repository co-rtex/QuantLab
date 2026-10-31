"""Dataset quality checks and ingestion gates for canonical bars and corporate actions.

Each check yields a status (``pass`` / ``warn`` / ``fail``), a count and a small sample of
offending rows, so the report remains useful for large datasets.

A ``fail`` is an **ingestion gate**: :func:`enforce_gates` raises
:class:`QualityGateError` and the dataset must not be registered. Gates cover inputs that
are malformed or would silently corrupt returns: invalid or duplicate bars, duplicate
actions, actions for unknown symbols or outside a symbol's history, impossible split
ratios or dividends, impossible action date order, and price moves that look like an
unrecorded or misrecorded split. A ``warn`` marks something a researcher should look at
but that the pipeline handles correctly: missing sessions, an ex-date without a bar,
genuine large moves and stale prices.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

from quantlab.data.adjust import _matrices, place_actions
from quantlab.data.calendar import trading_days
from quantlab.data.schema import ACTION_DATE_COLUMNS

Status = Literal["pass", "warn", "fail"]
SAMPLE = 5
# Gross one-period returns that an unrecorded (or extra) k-for-1 or 1-for-k split produces
# on an otherwise quiet day. 3-for-2 and smaller ratios are left out: a -33% day is too
# common in real data to block ingestion on.
SPLIT_SIGNATURES = (2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50, 100)


@dataclass
class QualityCheck:
    name: str
    status: Status
    count: int
    description: str
    sample: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class QualityReport:
    status: Status
    rows: int
    symbols: int
    start: str | None
    end: str | None
    checks: list[QualityCheck]
    per_symbol: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def blocking(self) -> list[QualityCheck]:
        return [c for c in self.checks if c.status == "fail"]

    def gate_summary(self) -> str:
        lines = [f"dataset rejected by {len(self.blocking)} ingestion gate(s):"]
        for c in self.blocking:
            example = f"; e.g. {c.sample[0]}" if c.sample else ""
            lines.append(f"- {c.name} ({c.count}): {c.description}{example}")
        return "\n".join(lines)


class QualityGateError(ValueError):
    """Raised when a dataset fails an ingestion gate; carries the full report."""

    def __init__(self, report: QualityReport) -> None:
        self.report = report
        super().__init__(report.gate_summary())


def enforce_gates(report: QualityReport) -> QualityReport:
    """Return ``report`` unchanged, or raise :class:`QualityGateError` if any check failed."""
    if report.blocking:
        raise QualityGateError(report)
    return report


def _sample(frame: pd.DataFrame) -> list[dict[str, Any]]:
    out = frame.head(SAMPLE).copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].astype(str)
    return [{k: (None if pd.isna(v) else v) for k, v in r.items()} for r in out.to_dict("records")]


def _check(name: str, bad: pd.DataFrame, description: str, fail: bool) -> QualityCheck:
    status: Status = "pass" if len(bad) == 0 else ("fail" if fail else "warn")
    return QualityCheck(name, status, len(bad), description, _sample(bad))


def assess_quality(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    *,
    extreme_return: float = 0.5,
    split_mismatch: float = 0.25,
    split_tolerance: float = 0.025,
    max_split_ratio: float = 10_000.0,
    stale_run: int = 5,
    as_of: pd.Timestamp | None = None,
) -> QualityReport:
    """Run every check on canonical (normalized) bars and actions.

    * ``extreme_return``: |total return| above this is reported (warning).
    * ``split_mismatch``: |total return| above this on a bar carrying a split means the
      ratio or the ex-date does not explain the price change (gate).
    * ``split_tolerance``: relative distance from a split signature (``SPLIT_SIGNATURES``)
      within which a gross return on a bar without a split is split-like (gate).
    * ``max_split_ratio``: a split ratio equal to 1 or outside ``[1/max, max]`` is invalid.
    """
    cols = ["symbol", "date"]
    checks: list[QualityCheck] = []

    dup = bars[bars.duplicated(cols, keep=False)]
    checks.append(_check("duplicate_bars", dup[cols], "Duplicate (symbol, date) rows", True))

    price_cols = ["open", "high", "low", "close"]
    nonfinite = bars[~np.isfinite(bars[[*price_cols, "volume"]]).all(axis=1)]
    checks.append(
        _check("non_finite_values", nonfinite[cols], "Missing or non-finite OHLCV values", True)
    )

    nonpos = bars[(bars[price_cols] <= 0).any(axis=1)]
    checks.append(_check("non_positive_prices", nonpos[cols], "Prices <= 0", True))

    neg_vol = bars[bars["volume"] < 0]
    checks.append(_check("negative_volume", neg_vol[cols], "Volume < 0", True))

    eps = 1e-9
    ohlc_bad = bars[
        (bars["high"] + eps < bars[["open", "close", "low"]].max(axis=1))
        | (bars["low"] - eps > bars[["open", "close", "high"]].min(axis=1))
    ]
    checks.append(
        _check(
            "ohlc_inconsistent",
            ohlc_bad[[*cols, *price_cols]],
            "High below open/close/low or low above open/close/high",
            True,
        )
    )

    non_session = bars[~bars["date"].isin(trading_days(bars["date"].min(), bars["date"].max()))]
    checks.append(
        _check("non_trading_day", non_session[cols], "Bars dated on non-trading days", False)
    )

    if as_of is not None:
        future = bars[bars["available_at"] > pd.Timestamp(as_of, tz="UTC")]
        checks.append(
            _check("future_availability", future[cols], "Bars not yet available as-of", True)
        )

    # Missing sessions inside each symbol's own [first, last] window.
    missing_rows = []
    per_symbol = []
    for sym, g in bars.groupby("symbol", sort=True):
        expected = trading_days(g["date"].min(), g["date"].max())
        missing = expected.difference(pd.DatetimeIndex(g["date"]))
        for d in missing[:SAMPLE]:
            missing_rows.append({"symbol": sym, "date": d})
        per_symbol.append(
            {
                "symbol": sym,
                "rows": len(g),
                "first": str(g["date"].min().date()),
                "last": str(g["date"].max().date()),
                "missing_sessions": len(missing),
                "coverage": round(len(g) / max(len(expected), 1), 6),
            }
        )
    n_missing = sum(p["missing_sessions"] for p in per_symbol)
    mc = QualityCheck(
        "missing_sessions",
        "pass" if n_missing == 0 else "warn",
        n_missing,
        "Trading sessions absent within a symbol's listing window",
        _sample(pd.DataFrame(missing_rows)) if missing_rows else [],
    )
    checks.append(mc)

    clean = bars.drop_duplicates(cols)
    close = clean.pivot(index="date", columns="symbol", values="close").sort_index()
    placed = place_actions(close, actions)
    checks.extend(_action_checks(actions, placed, max_split_ratio))
    checks.extend(_return_checks(close, placed, extreme_return, split_mismatch, split_tolerance))

    stale_rows = []
    for sym in close.columns:
        s = close[sym].dropna()
        if len(s) < stale_run:
            continue
        same = (s.diff() == 0).astype(int)
        runs = same.groupby((same == 0).cumsum()).cumsum()
        if (runs >= stale_run - 1).any():
            stale_rows.append({"symbol": sym, "date": runs.idxmax(), "run": int(runs.max()) + 1})
    checks.append(
        _check(
            "stale_prices",
            pd.DataFrame(stale_rows),
            f"Close unchanged for >= {stale_run} consecutive sessions",
            False,
        )
    )

    worst: Status = "pass"
    if any(c.status == "fail" for c in checks):
        worst = "fail"
    elif any(c.status == "warn" for c in checks):
        worst = "warn"
    return QualityReport(
        status=worst,
        rows=len(bars),
        symbols=int(bars["symbol"].nunique()),
        start=str(bars["date"].min().date()) if len(bars) else None,
        end=str(bars["date"].max().date()) if len(bars) else None,
        checks=checks,
        per_symbol=per_symbol,
    )


def _action_checks(
    actions: pd.DataFrame, placed: pd.DataFrame, max_split_ratio: float
) -> list[QualityCheck]:
    key = ["symbol", "ex_date", "kind"]
    show = [*key, "value"]
    splits = actions["kind"] == "split"
    # Two splits on one ex-date contradict each other even with different ratios; two
    # dividends with different amounts can be a regular and a special dividend.
    repeated = actions.duplicated([*key, "value"], keep=False) | (
        splits & actions.duplicated(key, keep=False)
    )
    v = actions["value"]
    bad_ratio = splits & (
        ((v - 1.0).abs() < 1e-12) | (v > max_split_ratio) | (v < 1.0 / max_split_ratio)
    )
    first_known = actions[["ex_date", "record_date", "pay_date"]].min(axis=1)
    misordered = (actions["declared_date"] > first_known) | (
        actions["pay_date"] < actions["record_date"]
    )
    return [
        _check(
            "duplicate_corporate_actions",
            actions.loc[repeated, show],
            "Corporate actions repeated on one ex-date (would be applied twice)",
            True,
        ),
        _check(
            "orphan_corporate_actions",
            placed.loc[placed["placement"] == "unknown_symbol", show],
            "Corporate actions for a symbol with no bars",
            True,
        ),
        _check(
            "corporate_actions_outside_history",
            placed.loc[placed["placement"] == "outside_history", show],
            "Corporate actions dated before a symbol's first bar or after its last bar",
            True,
        ),
        _check(
            "corporate_action_ex_date_without_bar",
            placed.loc[placed["placement"] == "next_bar", show],
            "Ex-date has no bar for the symbol; the action applies at its next bar",
            False,
        ),
        _check(
            "split_ratio_invalid",
            actions.loc[bad_ratio, show],
            f"Split ratio equal to 1 or outside [1/{max_split_ratio:g}, {max_split_ratio:g}]",
            True,
        ),
        _check(
            "corporate_action_date_order",
            actions.loc[misordered, [*show, *ACTION_DATE_COLUMNS]],
            "Declared after its ex, record or pay date, or paid before its record date",
            True,
        ),
    ]


def _return_checks(
    close: pd.DataFrame,
    placed: pd.DataFrame,
    extreme_return: float,
    split_mismatch: float,
    split_tolerance: float,
) -> list[QualityCheck]:
    """Checks on bar-to-bar total returns, computed exactly as ``adjust.total_returns``."""
    splits, divs = _matrices(close.shape, placed)
    c = close.to_numpy(dtype=float)
    prev = close.ffill().shift(1).to_numpy(dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = (splits * c + divs) / prev - 1.0
        log_gross = np.abs(np.log1p(ret))
    defined = np.isfinite(ret)
    has_split = splits != 1.0
    tol = np.log1p(split_tolerance)
    near = np.zeros(ret.shape, dtype=bool)
    for k in SPLIT_SIGNATURES:
        near |= np.abs(log_gross - np.log(k)) <= tol
    split_like = defined & ~has_split & near
    mismatch = defined & has_split & (np.abs(ret) > split_mismatch)
    with np.errstate(invalid="ignore"):
        div_too_big = (divs > 0) & ~(divs < prev)
    extreme = defined & (np.abs(ret) > extreme_return) & ~split_like & ~mismatch

    def cells(mask: np.ndarray, **extra: np.ndarray) -> pd.DataFrame:
        r, j = np.nonzero(mask)
        frame = pd.DataFrame(
            {
                "symbol": close.columns.to_numpy()[j],
                "date": close.index.to_numpy()[r],
                "prev_close": prev[r, j],
                "close": c[r, j],
                "ret": ret[r, j],
                **{name: values[r, j] for name, values in extra.items()},
            }
        )
        return frame.sort_values(["symbol", "date"]).reset_index(drop=True)

    return [
        _check(
            "split_like_returns",
            cells(split_like),
            "Price change matching a split ratio on a bar without a split (unrecorded split?)",
            True,
        ),
        _check(
            "split_price_mismatch",
            cells(mismatch, split=splits),
            f"|total return| > {split_mismatch:.0%} on a split bar (wrong ratio or ex-date?)",
            True,
        ),
        _check(
            "dividend_exceeds_price",
            cells(div_too_big, dividend=divs),
            "Cash dividend of at least the previous close (ex-dividend price would be <= 0)",
            True,
        ),
        _check(
            "extreme_returns",
            cells(extreme),
            f"|total return| > {extreme_return:.0%} after corporate-action adjustment "
            "and not split-like",
            False,
        ),
    ]
