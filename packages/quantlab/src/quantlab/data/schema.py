"""Canonical in-memory schemas for market data.

Bars are stored *raw* (as traded, unadjusted). Adjusted series are derived on demand
from corporate actions so that the as-of-date adjustment can be computed without
using information from the future.

Bars frame columns:
    symbol (str), date (datetime64, session date), open, high, low, close (float64),
    volume (float64), available_at (datetime64[UTC]: when the bar became knowable)

Corporate actions frame columns:
    symbol (str), ex_date (datetime64), kind ("split" | "dividend"), value (float64)
    - split: ratio of new shares per old share (2.0 = 2-for-1)
    - dividend: cash amount per pre-split share, paid to holders at the ex-date open
"""

from __future__ import annotations

import numpy as np
import pandas as pd

BAR_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "available_at"]
ACTION_COLUMNS = ["symbol", "ex_date", "kind", "value"]
ACTION_KINDS = ("split", "dividend")
# Daily bars are considered knowable at the regular-session close (16:00 New York).
SESSION_CLOSE = pd.Timedelta(hours=16)
EXCHANGE_TZ = "America/New_York"
# Tickers are short and plain; anything else is rejected at ingestion.
SYMBOL_PATTERN = r"[A-Z0-9][A-Z0-9._-]{0,19}"


class SchemaError(ValueError):
    """Raised when a market-data frame violates the canonical schema."""


def session_close_utc(dates: pd.Series | pd.DatetimeIndex) -> pd.Series:
    idx = pd.DatetimeIndex(dates)
    local = (idx.normalize() + SESSION_CLOSE).tz_localize(EXCHANGE_TZ)
    return pd.Series(local.tz_convert("UTC"))


def normalize_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """Coerce a provider frame into canonical form, sorted by (symbol, date).

    Timestamps are normalized to session dates; timezone-aware inputs are converted to
    exchange-local time first so that a UTC midnight timestamp does not shift sessions.
    """
    missing = {"symbol", "date", "open", "high", "low", "close", "volume"} - set(frame.columns)
    if missing:
        raise SchemaError(f"bars frame missing columns: {sorted(missing)}")
    out = frame.copy()
    dates = pd.to_datetime(out["date"])
    if dates.dt.tz is not None:
        dates = dates.dt.tz_convert(EXCHANGE_TZ).dt.tz_localize(None)
    out["date"] = dates.dt.normalize().astype("datetime64[ns]")
    out["symbol"] = out["symbol"].astype(str).str.upper().str.strip()
    bad = ~out["symbol"].str.fullmatch(SYMBOL_PATTERN)
    if bad.any():
        raise SchemaError(f"invalid symbols: {sorted(out.loc[bad, 'symbol'].unique())[:5]}")
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    if "available_at" not in out.columns or out["available_at"].isna().any():
        out["available_at"] = session_close_utc(out["date"]).to_numpy()
    else:
        out["available_at"] = pd.to_datetime(out["available_at"], utc=True)
    out["available_at"] = out["available_at"].astype("datetime64[ns, UTC]")
    out = out[BAR_COLUMNS].sort_values(["symbol", "date"], kind="stable")
    return out.reset_index(drop=True)


def normalize_actions(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or len(frame) == 0:
        return pd.DataFrame(
            {
                "symbol": pd.Series(dtype=str),
                "ex_date": pd.Series(dtype="datetime64[ns]"),
                "kind": pd.Series(dtype=str),
                "value": pd.Series(dtype="float64"),
            }
        )
    missing = set(ACTION_COLUMNS) - set(frame.columns)
    if missing:
        raise SchemaError(f"actions frame missing columns: {sorted(missing)}")
    out = frame.copy()
    out["symbol"] = out["symbol"].astype(str).str.upper().str.strip()
    if (~out["symbol"].str.fullmatch(SYMBOL_PATTERN)).any():
        raise SchemaError("invalid symbols in corporate actions")
    out["ex_date"] = pd.to_datetime(out["ex_date"]).dt.normalize().astype("datetime64[ns]")
    out["kind"] = out["kind"].astype(str).str.lower()
    out["value"] = pd.to_numeric(out["value"], errors="coerce").astype("float64")
    bad_kind = ~out["kind"].isin(ACTION_KINDS)
    if bad_kind.any():
        raise SchemaError(f"unknown corporate action kinds: {sorted(out.loc[bad_kind, 'kind'])}")
    if not np.isfinite(out["value"]).all() or (out["value"] <= 0).any():
        raise SchemaError("corporate action values must be finite and positive")
    out = out[ACTION_COLUMNS].sort_values(["symbol", "ex_date", "kind"], kind="stable")
    return out.reset_index(drop=True)


def wide(bars: pd.DataFrame, field: str = "close") -> pd.DataFrame:
    """Pivot a canonical bars frame to a date x symbol matrix."""
    return bars.pivot(index="date", columns="symbol", values=field).sort_index()
