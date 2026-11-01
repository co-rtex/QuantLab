"""Historical bar retrieval over immutable Parquet versions (DuckDB) with Redis caching.

Dataset versions are content-addressed and never change, so cache entries keyed by the
version hash never need invalidation; a TTL only bounds memory use.
"""

from __future__ import annotations

import json
import threading
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from quantlab.data.adjust import PointInTimeAdjuster
from quantlab.data.schema import normalize_actions
from quantlab.data.vendors.http import Transport, urllib_transport
from quantlab.data.vendors.massive import MassiveProvider
from quantlab.data.vendors.registry import get_vendor
from quantlab_api.config import Settings
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


@lru_cache(maxsize=256)
def bar_dates(path: Path, end: date | None = None) -> pd.DatetimeIndex:
    """Distinct bar dates in a dataset version, up to ``end``, sorted: the sessions the
    engine steps through and rebalances on, which can include days the exchange calendar
    does not (#163, #181).

    PyArrow, not DuckDB: this runs at submission, and a DuckDB connection does not survive
    a fork (its threads stay behind), so one opened here would hang a work horse forked
    from the same process, as the tests run API and worker. Cached: a version's files are
    immutable (content-addressed), so the dates for a path and end never change. An index
    is immutable, so the cached value cannot be altered by a caller.
    """
    dates = pq.read_table(path / "bars.parquet", columns=["date"]).column("date")
    if end is not None:
        dates = dates.filter(pc.less_equal(dates, pa.scalar(pd.Timestamp(end), dates.type)))
    return pd.DatetimeIndex(pd.to_datetime(pc.unique(dates).to_pandas())).sort_values()


def query_bars(path: Path, symbol: str, start: date | None, end: date | None) -> pd.DataFrame:
    where = ["symbol = ?"]
    params: list[Any] = [str(path / "bars.parquet"), symbol.upper()]
    if start is not None:
        where.append("date >= CAST(? AS TIMESTAMP_NS)")
        params.append(str(start))
    if end is not None:
        where.append("date <= CAST(? AS TIMESTAMP_NS)")
        params.append(str(end))
    # Only fixed predicate fragments are interpolated; values are bound parameters.
    cols = "date, open, high, low, close, volume"
    predicate = " AND ".join(where)
    sql = f"SELECT {cols} FROM read_parquet(?) WHERE {predicate} ORDER BY date"  # noqa: S608
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


# ------------------------------------------------------------ licensed vendors (#11)
class MarketDataNotConfiguredError(Exception):
    """No licensed market-data provider is configured; the vendor path stays inert."""


def vendor_status(settings: Settings) -> dict[str, Any]:
    """Whether the vendor path is enabled, and if not, which settings are unset.

    Never includes the key itself, only whether it is set.
    """
    missing = [
        env
        for env, value in (
            ("QUANTLAB_MARKET_DATA_PROVIDER", settings.market_data_provider),
            ("QUANTLAB_MARKET_DATA_API_KEY", settings.market_data_api_key),
            ("QUANTLAB_MARKET_DATA_LICENSE", settings.market_data_license),
            ("QUANTLAB_MARKET_DATA_PLAN", settings.market_data_plan),
        )
        if value is None
    ]
    return {"enabled": not missing, "provider": settings.market_data_provider, "missing": missing}


def configured_vendor(settings: Settings, transport: Transport | None = None) -> MassiveProvider:
    """The configured vendor's provider; MarketDataNotConfiguredError names what is unset.

    The vendor and its licence are the user's choice: until all four settings are present
    this refuses, so nothing fetches from a vendor by default.
    """
    status = vendor_status(settings)
    if not status["enabled"]:
        raise MarketDataNotConfiguredError(
            "licensed market data is not configured (unset: " + ", ".join(status["missing"]) + ")"
        )
    assert settings.market_data_provider
    assert settings.market_data_api_key
    assert settings.market_data_license
    assert settings.market_data_plan
    return get_vendor(settings.market_data_provider).build(
        settings.market_data_api_key.get_secret_value(),
        license=settings.market_data_license,
        plan=settings.market_data_plan,
        requests_per_minute=settings.market_data_requests_per_minute,
        transport=transport or urllib_transport,
    )
