"""Historical bar retrieval over immutable Parquet versions (DuckDB) with Redis caching.

Dataset versions are content-addressed and never change, so cache entries keyed by the
version hash never need invalidation; a TTL only bounds memory use.
"""

from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from quantlab.data.adjust import PointInTimeAdjuster
from quantlab.data.schema import normalize_actions
from quantlab_api.observability import CACHE_EVENTS, log

_local = threading.local()
CACHE_TTL_SECONDS = 6 * 3600


def _duck() -> duckdb.DuckDBPyConnection:
    con = getattr(_local, "con", None)
    if con is None:
        con = duckdb.connect(database=":memory:")
        con.execute("SET threads TO 2")
        _local.con = con
    return con


def query_bars(path: Path, symbol: str, start: date | None, end: date | None) -> pd.DataFrame:
    where = ["symbol = ?"]
    params: list[Any] = [str(path / "bars.parquet"), symbol.upper()]
    if start is not None:
        where.append("date >= CAST(? AS TIMESTAMP_NS)")
        params.append(str(start))
    if end is not None:
        where.append("date <= CAST(? AS TIMESTAMP_NS)")
        params.append(str(end))
    sql = (
        "SELECT date, open, high, low, close, volume FROM read_parquet(?) "
        f"WHERE {' AND '.join(where)} ORDER BY date"  # noqa: S608 - fixed fragments only
    )
    return _duck().execute(sql, params).df()


def query_actions(path: Path, symbol: str) -> pd.DataFrame:
    sql = "SELECT symbol, ex_date, kind, value FROM read_parquet(?) WHERE symbol = ?"
    return _duck().execute(sql, [str(path / "actions.parquet"), symbol.upper()]).df()


def adjust_frame(bars: pd.DataFrame, actions: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Total-return adjustment as of the last bar in the frame."""
    if len(bars) == 0:
        return bars
    close = bars.set_index("date")[["close"]].rename(columns={"close": symbol})
    adj = PointInTimeAdjuster(close, normalize_actions(actions))
    mult = adj.multiplier(None)[:, 0]
    out = bars.copy()
    for c in ("open", "high", "low", "close"):
        out[c] = out[c].to_numpy() * mult
    return out


def bars_payload(
    path: Path,
    version_hash: str,
    symbol: str,
    start: date | None,
    end: date | None,
    adjusted: bool,
    redis: Any | None = None,
) -> dict[str, Any]:
    key = f"ql:bars:{version_hash}:{symbol.upper()}:{start}:{end}:{int(adjusted)}"
    if redis is not None:
        try:
            hit = redis.get(key)
            if hit is not None:
                CACHE_EVENTS.labels("bars", "hit").inc()
                data: dict[str, Any] = json.loads(hit)
                data["cache"] = "hit"
                return data
            CACHE_EVENTS.labels("bars", "miss").inc()
        except Exception as exc:  # cache is an optimization; never fail the read
            CACHE_EVENTS.labels("bars", "error").inc()
            log.warning("cache_error", error=str(exc))
            redis = None
    df = query_bars(path, symbol, start, end)
    if adjusted and len(df):
        # Adjustment must use every event up to the last bar of the full history, not just
        # the requested window, so load the full series for the factor computation.
        full = query_bars(path, symbol, None, end)
        adj = adjust_frame(full, query_actions(path, symbol), symbol.upper())
        df = adj if start is None else adj[adj["date"] >= pd.Timestamp(start)]
        df = df.reset_index(drop=True)

    def col(name: str) -> list[float | None]:
        arr = df[name].to_numpy(dtype=float)
        return [None if not np.isfinite(v) else round(float(v), 6) for v in arr]

    payload = {
        "symbol": symbol.upper(),
        "adjusted": adjusted,
        "dates": [d.strftime("%Y-%m-%d") for d in pd.to_datetime(df["date"])],
        "open": col("open"),
        "high": col("high"),
        "low": col("low"),
        "close": col("close"),
        "volume": col("volume"),
    }
    if redis is not None:
        try:
            redis.set(key, json.dumps(payload), ex=CACHE_TTL_SECONDS)
        except Exception as exc:
            log.warning("cache_error", error=str(exc))
    payload["cache"] = "miss" if redis is not None else "bypass"
    return payload
