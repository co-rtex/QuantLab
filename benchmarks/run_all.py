"""Reproducible performance benchmarks.

Usage::

    make up                                   # Postgres + Redis
    uv run python benchmarks/run_all.py --profile small|medium|large [--api URL]

Writes ``benchmarks/results/<timestamp>-<profile>.json``. Every number is measured in this
run; nothing is estimated. Hardware and software versions are recorded with the results.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import statistics
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
PROFILES = {
    "small": {"assets": 100, "years": 6, "queries": 100, "parallel_runs": 8},
    "medium": {"assets": 500, "years": 6, "queries": 200, "parallel_runs": 8},
    "large": {"assets": 1000, "years": 10, "queries": 200, "parallel_runs": 8},
    "xl": {"assets": 2000, "years": 22, "queries": 200, "parallel_runs": 4},
}


def pct(xs: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(xs), q))


def summarize(samples: list[float]) -> dict[str, float]:
    ms = [s * 1000 for s in samples]
    return {
        "n": len(ms),
        "p50_ms": round(pct(ms, 50), 3),
        "p95_ms": round(pct(ms, 95), 3),
        "p99_ms": round(pct(ms, 99), 3),
        "mean_ms": round(statistics.fmean(ms), 3),
    }


def maxrss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1024 / 1024 if sys.platform == "darwin" else r / 1024


def environment() -> dict[str, Any]:
    import duckdb
    import pyarrow

    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "duckdb": duckdb.__version__,
        "pyarrow": pyarrow.__version__,
        "memory_gb": round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1),
    }


def _warm(_: int) -> None:
    import quantlab.backtest.engine
    import quantlab.strategies.library  # noqa: F401


def _run_one(args: tuple[str, int]) -> float:
    path, seed = args
    from quantlab.backtest.engine import BacktestConfig, run_backtest
    from quantlab.strategies.library import create_strategy

    bars = pd.read_parquet(Path(path) / "bars.parquet")
    actions = pd.read_parquet(Path(path) / "actions.parquet")
    t = time.perf_counter()
    run_backtest(
        bars,
        actions,
        create_strategy("momentum", {"top_n": 10 + seed % 5}),
        BacktestConfig(start=str(bars["date"].min().year + 1) + "-01-02", benchmark="SYNIDX"),
        record_positions=False,
    )
    return time.perf_counter() - t


def bench(
    profile: str, api: str | None, skip_parallel: bool = False, skip_postgres: bool = False
) -> dict[str, Any]:
    from quantlab.backtest.engine import BacktestConfig, run_backtest
    from quantlab.data.quality import assess_quality
    from quantlab.data.store import DatasetStore
    from quantlab.data.synthetic import SyntheticSpec, generate_market
    from quantlab.strategies.library import create_strategy

    p = PROFILES[profile]
    out: dict[str, Any] = {"profile": profile, "params": p, "environment": environment()}
    end_year = 2024
    spec = SyntheticSpec(
        n_assets=p["assets"],
        start=f"{end_year - p['years']}-01-02",
        end=f"{end_year - 1}-12-29",
        seed=1,
    )
    t = time.perf_counter()
    m = generate_market(spec)
    out["data"] = {
        "rows": len(m.bars),
        "symbols": int(m.bars["symbol"].nunique()),
        "generate_s": round(time.perf_counter() - t, 3),
    }
    t = time.perf_counter()
    q = assess_quality(m.bars, m.actions)
    out["data"]["quality_s"] = round(time.perf_counter() - t, 3)
    out["data"]["quality_status"] = q.status
    out["data"]["quality_findings"] = {c.name: c.count for c in q.checks if c.status != "pass"}
    tmp = Path(tempfile.mkdtemp(prefix="qlbench-"))
    store = DatasetStore(tmp)
    t = time.perf_counter()
    dv = store.write("bench", m.bars, m.actions, m.provenance)
    out["data"]["write_parquet_s"] = round(time.perf_counter() - t, 3)
    out["data"]["parquet_mb"] = round((dv.path / "bars.parquet").stat().st_size / 2**20, 2)

    # --- engine: one backtest per universe size (5 trading years, monthly rebalance) ------
    engine = []
    trade_start = f"{end_year - 5}-01-02"
    for n in sorted({min(100, p["assets"]), p["assets"]}):
        universe = sorted(m.bars["symbol"].unique().tolist())
        universe = [s for s in universe if s != "SYNIDX"][:n]
        for key in ("equal_weight", "momentum"):
            cfg = BacktestConfig(start=trade_start, universe=universe, benchmark="SYNIDX")
            t = time.perf_counter()
            res = run_backtest(m.bars, m.actions, create_strategy(key), cfg)
            engine.append(
                {
                    "assets": n,
                    "strategy": key,
                    "sessions": res.meta["sessions"],
                    "trades": len(res.trades),
                    "seconds": round(time.perf_counter() - t, 3),
                }
            )
    out["engine"] = engine
    out["engine_peak_rss_mb"] = round(maxrss_mb(), 1)

    if skip_parallel:
        out["parallel"] = {"skipped": True}
    else:
        # --- serial vs parallel independent backtests ----------------------------------------
        runs = p["parallel_runs"]
        t = time.perf_counter()
        serial = [_run_one((str(dv.path), i)) for i in range(runs)]
        serial_wall = time.perf_counter() - t
        par = {}
        for workers in (2, 4):
            with ProcessPoolExecutor(max_workers=workers) as ex:
                # Warm the pool first: production workers are long-lived processes, so
                # interpreter start-up and imports are not part of per-job cost.
                list(ex.map(_warm, range(workers)))
                t = time.perf_counter()
                list(ex.map(_run_one, [(str(dv.path), i) for i in range(runs)]))
                par[workers] = round(time.perf_counter() - t, 3)
        out["parallel"] = {
            "runs": runs,
            "serial_wall_s": round(serial_wall, 3),
            "mean_single_run_s": round(statistics.fmean(serial), 3),
            "workers_wall_s": par,
            "speedup_4": round(serial_wall / par[4], 2),
        }

    # --- historical retrieval: one symbol, last five years ------------------------------
    rng = np.random.default_rng(0)
    # Without replacement: a repeated symbol would be a cache hit inside the "miss" loop.
    universe = sorted(m.bars["symbol"].unique())
    syms = rng.choice(universe, size=min(p["queries"], len(universe)), replace=False)
    since = pd.Timestamp(trade_start)
    from quantlab_api import marketdata

    duck = []
    for s in syms:
        t = time.perf_counter()
        marketdata.query_bars(dv.path, s, since.date(), None)
        duck.append(time.perf_counter() - t)
    arrow = []
    for s in syms[: max(20, len(syms) // 4)]:
        t = time.perf_counter()
        store.load_bars(dv, symbols=[s], start=str(since.date()))
        arrow.append(time.perf_counter() - t)
    out["retrieval"] = {"duckdb_parquet": summarize(duck), "pyarrow_filters": summarize(arrow)}
    if not skip_postgres:
        out["retrieval"].update(postgres_rows(m.bars, syms, since))
    out["retrieval"].update(redis_cache(dv, syms))
    if api:
        out["api"] = api_latency(api, dv)
    return out


def postgres_rows(bars: pd.DataFrame, syms: np.ndarray, since: pd.Timestamp) -> dict[str, Any]:
    """Row-oriented storage in PostgreSQL, unindexed vs (symbol, date) B-tree index."""
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        return {}
    url = os.environ.get(
        "QUANTLAB_BENCH_PG", "postgresql://quantlab:quantlab@localhost:5433/quantlab"
    )
    try:
        conn = psycopg.connect(url, autocommit=True)
    except Exception as exc:  # pragma: no cover
        return {"postgres_error": str(exc)}
    with conn:
        conn.execute("DROP TABLE IF EXISTS bench_bars")
        conn.execute(
            "CREATE UNLOGGED TABLE bench_bars (symbol text, date date, open float8, "
            "high float8, low float8, close float8, volume float8)"
        )
        t = time.perf_counter()
        with conn.cursor().copy("COPY bench_bars FROM STDIN") as cp:
            for row in bars[
                ["symbol", "date", "open", "high", "low", "close", "volume"]
            ].itertuples(index=False):
                cp.write_row(
                    (
                        row.symbol,
                        row.date.date(),
                        row.open,
                        row.high,
                        row.low,
                        row.close,
                        row.volume,
                    )
                )
        load_s = time.perf_counter() - t
        conn.execute("ANALYZE bench_bars")
        sql = (
            "SELECT date, open, high, low, close, volume FROM bench_bars "
            "WHERE symbol = %s AND date >= %s ORDER BY date"
        )

        def run(n: int) -> list[float]:
            xs = []
            for s in syms[:n]:
                t0 = time.perf_counter()
                conn.execute(sql, (str(s), since.date())).fetchall()
                xs.append(time.perf_counter() - t0)
            return xs

        unindexed = run(min(30, len(syms)))
        t = time.perf_counter()
        conn.execute("CREATE INDEX ON bench_bars (symbol, date)")
        index_s = time.perf_counter() - t
        conn.execute("ANALYZE bench_bars")
        indexed = run(len(syms))
        conn.execute("DROP TABLE bench_bars")
    return {
        "postgres_unindexed": summarize(unindexed),
        "postgres_indexed": summarize(indexed),
        "postgres_copy_load_s": round(load_s, 2),
        "postgres_index_build_s": round(index_s, 2),
    }


def redis_cache(dv: Any, syms: np.ndarray) -> dict[str, Any]:
    from redis import Redis

    from quantlab_api.marketdata import bars_payload

    try:
        r = Redis.from_url(os.environ.get("QUANTLAB_REDIS_URL", "redis://localhost:6380/0"))
        r.ping()
    except Exception as exc:  # pragma: no cover
        return {"redis_error": str(exc)}
    for k in r.scan_iter(f"ql:bars:{dv.version}:*"):
        r.delete(k)
    miss, hit = [], []
    for s in syms:
        t = time.perf_counter()
        bars_payload(dv.path, dv.version, s, None, None, True, r)
        miss.append(time.perf_counter() - t)
    for s in syms:
        t = time.perf_counter()
        bars_payload(dv.path, dv.version, s, None, None, True, r)
        hit.append(time.perf_counter() - t)
    return {
        "adjusted_full_history_uncached": summarize(miss),
        "adjusted_full_history_cached": summarize(hit),
    }


def api_latency(api: str, dv: Any) -> dict[str, Any]:
    """End-to-end HTTP latency against a running API (lightweight authenticated reads)."""
    import httpx

    c = httpx.Client(base_url=api, timeout=30)
    email = f"bench-{int(time.time())}@example.com"
    tok = c.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "Bench-mark-2026", "display_name": "bench"},
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    ws = c.get("/api/v1/workspaces", headers=h).json()[0]["id"]
    out = {}
    for name, path in [
        ("health", "/api/v1/health"),
        ("workspaces", "/api/v1/workspaces"),
        ("experiments_list", f"/api/v1/workspaces/{ws}/experiments"),
        ("strategy_catalog", "/api/v1/strategies/catalog"),
    ]:
        xs = []
        for _ in range(200):
            t = time.perf_counter()
            r = c.get(path, headers=h)
            xs.append(time.perf_counter() - t)
            assert r.status_code == 200, r.text
        out[name] = summarize(xs)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=sorted(PROFILES), default="small")
    ap.add_argument(
        "--api", default=None, help="base URL of a running API, e.g. http://localhost:8000"
    )
    ap.add_argument("--skip-parallel", action="store_true")
    ap.add_argument("--skip-postgres", action="store_true")
    args = ap.parse_args()
    res = bench(args.profile, args.api, args.skip_parallel, args.skip_postgres)
    res["peak_rss_mb"] = round(maxrss_mb(), 1)
    outdir = ROOT / "results"
    outdir.mkdir(exist_ok=True)
    path = outdir / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{args.profile}.json"
    path.write_text(json.dumps(res, indent=2, default=str))
    print(json.dumps(res, indent=2, default=str))
    print(f"\nwritten {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
