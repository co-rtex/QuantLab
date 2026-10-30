"""Dataset quality checks for canonical OHLCV bars.

Each check yields a status (``pass`` / ``warn`` / ``fail``), a count and a small sample of
offending rows, so the report remains useful for large datasets.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

from quantlab.data.adjust import total_returns
from quantlab.data.calendar import trading_days

Status = Literal["pass", "warn", "fail"]
SAMPLE = 5


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
    stale_run: int = 5,
    as_of: pd.Timestamp | None = None,
) -> QualityReport:
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
    tr = total_returns(clean, actions)
    stacked = tr.stack().rename("ret").reset_index()
    extreme = stacked[stacked["ret"].abs() > extreme_return]
    checks.append(
        _check(
            "extreme_returns",
            extreme,
            f"|total return| > {extreme_return:.0%} after corporate-action adjustment",
            False,
        )
    )

    close = clean.pivot(index="date", columns="symbol", values="close")
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

    if len(actions):
        orphan = actions[
            ~actions.set_index(["symbol", "ex_date"]).index.isin(
                clean.set_index(["symbol", "date"]).index
            )
        ]
    else:
        orphan = actions
    checks.append(
        _check(
            "orphan_corporate_actions",
            orphan,
            "Corporate actions whose ex-date has no matching bar",
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
