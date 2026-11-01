"""Reproducible performance benchmarks.

Usage::

    make up                                   # Postgres + Redis
    uv run python benchmarks/run_all.py --profile small|medium|large [--repeats 5]
        [--cold] [--api URL] [--skip-parallel] [--skip-postgres] [--skip-redis]

Writes ``benchmarks/results/<timestamp>-<profile>[-contended].json``. Every number is
measured in this run; nothing is estimated. The measurement rules (repeats, median and
IQR with raw samples, machine and load metadata, warm vs cold, in-process labels, warmed
pools) are in ``benchmarks/harness.py``. A result marked ``contended`` was taken on a
loaded machine and is not published.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness

ROOT = Path(__file__).resolve().parent
PROFILES = {
    "tiny": {"assets": 20, "years": 3, "queries": 10, "parallel_runs": 2},  # tests only
    "small": {"assets": 100, "years": 6, "queries": 100, "parallel_runs": 8},
    "medium": {"assets": 500, "years": 6, "queries": 200, "parallel_runs": 8},
    "large": {"assets": 1000, "years": 10, "queries": 200, "parallel_runs": 8},
    "xl": {"assets": 2000, "years": 22, "queries": 200, "parallel_runs": 4},
}


def maxrss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1024 / 1024 if sys.platform == "darwin" else r / 1024


def libraries() -> dict[str, str]:
    import duckdb
    import pyarrow

    return {
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "duckdb": duckdb.__version__,
        "pyarrow": pyarrow.__version__,
    }


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


def sample_symbols(universe: list[str], n: int, repeat_index: int) -> list[str]:
    """``n`` distinct symbols for one repeat (the #1 fix: never with replacement).

    Each repeat draws its own sample (seeded by its index), so repeats are independent
    and a repeated symbol can never turn a measured miss into a cache hit.
    """
    rng = np.random.default_rng(repeat_index)
    k = min(n, len(universe))
    return [str(s) for s in rng.choice(sorted(universe), size=k, replace=False)]


def bench(
    profile: str,
    api: str | None,
    *,
    repeats: int = 5,
    cold: bool = False,
    skip_parallel: bool = False,
    skip_postgres: bool = False,
    skip_redis: bool = False,
    settle_s: float = harness.SETTLE_S,
) -> dict[str, Any]:
    from quantlab.backtest.engine import BacktestConfig, run_backtest
    from quantlab.data.quality import assess_quality
    from quantlab.data.store import DatasetStore
    from quantlab.data.synthetic import SyntheticSpec, generate_market
    from quantlab.strategies.library import create_strategy

    start_load = harness.load_snapshot()
    drop, cache_note = harness.cold_capability() if cold else (None, "warm (not requested)")
    cache = "cold" if drop else "warm"
    p = PROFILES[profile]
    out: dict[str, Any] = {
        "profile": profile,
        "params": p,
        "repeats": repeats,
        "cache": {"mode": cache, "note": cache_note},
        "environment": {**harness.machine(), **libraries()},
        "data_is_synthetic": True,
    }
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
        "generate_s_single": round(time.perf_counter() - t, 3),  # setup, timed once
        "quality": harness.stats(
            harness.repeat(lambda: assess_quality(m.bars, m.actions), repeats),
            in_process=True,
            cache="n/a",
        ),
    }
    q = assess_quality(m.bars, m.actions)
    out["data"]["quality_status"] = q.status
    tmp = Path(tempfile.mkdtemp(prefix="qlbench-"))

    def fresh_write() -> None:  # a new store each time: a version is written only once
        DatasetStore(Path(tempfile.mkdtemp(dir=tmp))).write(
            "bench", m.bars, m.actions, m.provenance
        )

    out["data"]["write_parquet"] = harness.stats(
        harness.repeat(fresh_write, repeats), in_process=True, cache="n/a"
    )
    store = DatasetStore(tmp / "main")
    dv = store.write("bench", m.bars, m.actions, m.provenance)
    out["data"]["parquet_mb"] = round((dv.path / "bars.parquet").stat().st_size / 2**20, 2)

    # --- engine: one backtest per universe size (5 trading years, monthly rebalance) ------
    engine = []
    trade_start = f"{end_year - min(5, p['years'] - 1)}-01-02"
    universe_all = sorted(s for s in m.bars["symbol"].unique() if s != "SYNIDX")
    for n in sorted({min(100, len(universe_all)), len(universe_all)}):
        universe = universe_all[:n]
        for key in ("equal_weight", "momentum"):
            cfg = BacktestConfig(start=trade_start, universe=universe, benchmark="SYNIDX")
            res = run_backtest(m.bars, m.actions, create_strategy(key), cfg)  # warm-up

            def once(key: str = key, cfg: BacktestConfig = cfg) -> None:
                run_backtest(m.bars, m.actions, create_strategy(key), cfg)

            samples = harness.repeat(once, repeats)
            engine.append(
                {
                    "assets": n,
                    "strategy": key,
                    "sessions": res.meta["sessions"],
                    "trades": len(res.trades),
                    **harness.stats(samples, in_process=True, cache="n/a"),
                }
            )
    out["engine"] = engine
    out["engine_peak_rss_mb"] = round(maxrss_mb(), 1)

    if skip_parallel:
        out["parallel"] = {"skipped": True}
    else:
        out["parallel"] = parallel(str(dv.path), p["parallel_runs"], repeats)

    # --- historical retrieval: one symbol, recent years ---------------------------------
    from quantlab_api import marketdata

    since = pd.Timestamp(trade_start)
    duck: list[float] = []
    arrow: list[float] = []
    per_repeat: dict[str, list[float]] = {"duckdb_parquet": [], "pyarrow_filters": []}
    for r in range(repeats):
        syms = sample_symbols(universe_all, p["queries"], r)
        if drop:
            drop()
        d = harness.repeat_each(
            lambda s: marketdata.query_bars(dv.path, s, since.date(), None), syms
        )
        a = harness.repeat_each(
            lambda s: store.load_bars(dv, symbols=[s], start=str(since.date())),
            syms[: max(5, len(syms) // 4)],
        )
        duck += d
        arrow += a
        per_repeat["duckdb_parquet"].append(round(float(np.median(d)) * 1000, 3))
        per_repeat["pyarrow_filters"].append(round(float(np.median(a)) * 1000, 3))
    out["retrieval"] = {
        "duckdb_parquet": {
            **harness.stats(duck, in_process=True, cache=cache),
            "repeat_medians_ms": per_repeat["duckdb_parquet"],
        },
        "pyarrow_filters": {
            **harness.stats(arrow, in_process=True, cache=cache),
            "repeat_medians_ms": per_repeat["pyarrow_filters"],
        },
        "sampling": "without replacement, a fresh seeded sample per repeat",
    }
    if not skip_postgres:
        out["retrieval"].update(postgres_rows(m.bars, universe_all, p["queries"], since, repeats))
    if not skip_redis:
        out["retrieval"].update(redis_cache(dv, universe_all, p["queries"], repeats))
    if api:
        out["api"] = api_latency(api, repeats)
    out["peak_rss_mb"] = round(maxrss_mb(), 1)
    return harness.finalize(out, start_load, settle_s)


def parallel(path: str, runs: int, repeats: int) -> dict[str, Any]:
    """Serial vs pooled independent backtests; every pool is warmed before timing."""
    serial_walls: list[float] = []
    singles: list[float] = []
    for _ in range(repeats):
        t = time.perf_counter()
        singles += [_run_one((path, i)) for i in range(runs)]
        serial_walls.append(time.perf_counter() - t)
    out: dict[str, Any] = {
        "runs": runs,
        "serial_wall": harness.stats(serial_walls, in_process=True, cache="n/a"),
        "single_run": harness.stats(singles, in_process=True, cache="n/a"),
        "workers": {},
    }
    for workers in (2, 4):
        with harness.warmed_pool(workers) as (ex, warmed):

            def batch(ex: Any = ex) -> None:
                list(ex.map(_run_one, [(path, i) for i in range(runs)]))

            walls = harness.repeat(batch, repeats)
        out["workers"][str(workers)] = {
            "warmed_workers": warmed,
            **harness.stats(walls, in_process=False, cache="n/a"),  # worker processes
        }
    med4 = out["workers"]["4"]["median_ms"]
    out["speedup_4_median"] = round(out["serial_wall"]["median_ms"] / med4, 2) if med4 else None
    return out


def postgres_rows(
    bars: pd.DataFrame, universe: list[str], queries: int, since: pd.Timestamp, repeats: int
) -> dict[str, Any]:
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
            xs: list[float] = []
            for r in range(repeats):
                xs += harness.repeat_each(
                    lambda s: conn.execute(sql, (s, since.date())).fetchall(),
                    sample_symbols(universe, n, r),
                )
            return xs

        unindexed = run(min(30, queries))
        t = time.perf_counter()
        conn.execute("CREATE INDEX ON bench_bars (symbol, date)")
        index_s = time.perf_counter() - t
        conn.execute("ANALYZE bench_bars")
        indexed = run(queries)
        conn.execute("DROP TABLE bench_bars")
    return {
        "postgres_unindexed": harness.stats(unindexed, in_process=False, cache="warm"),
        "postgres_indexed": harness.stats(indexed, in_process=False, cache="warm"),
        "postgres_copy_load_s_single": round(load_s, 2),
        "postgres_index_build_s_single": round(index_s, 2),
    }


def redis_cache(dv: Any, universe: list[str], queries: int, repeats: int) -> dict[str, Any]:
    from redis import Redis

    from quantlab_api.marketdata import bars_payload

    try:
        r = Redis.from_url(os.environ.get("QUANTLAB_REDIS_URL", "redis://localhost:6380/0"))
        r.ping()
    except Exception as exc:  # pragma: no cover
        return {"redis_error": str(exc)}
    miss: list[float] = []
    hit: list[float] = []
    for k in range(repeats):
        for key in r.scan_iter(f"ql:bars:{dv.version}:*"):  # every miss is a real miss
            r.delete(key)
        syms = sample_symbols(universe, queries, k)

        def fetch(s: str) -> None:
            bars_payload(dv.path, dv.version, s, None, None, True, r)

        miss += harness.repeat_each(fetch, syms)
        hit += harness.repeat_each(fetch, syms)
    return {
        "adjusted_full_history_uncached": harness.stats(miss, in_process=True, cache="redis miss"),
        "adjusted_full_history_cached": harness.stats(hit, in_process=True, cache="redis hit"),
    }


def api_latency(api: str, repeats: int) -> dict[str, Any]:
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
        c.get(path, headers=h)  # warm the connection and the route

        def get(_: Any, path: str = path) -> None:
            resp = c.get(path, headers=h)
            assert resp.status_code == 200, resp.text

        out[name] = harness.stats(
            harness.repeat_each(get, range(200 * repeats)), in_process=False, cache="warm"
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=sorted(PROFILES), default="small")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--cold", action="store_true", help="drop OS caches per repeat (opt-in)")
    ap.add_argument(
        "--api", default=None, help="base URL of a running API, e.g. http://localhost:8000"
    )
    ap.add_argument("--skip-parallel", action="store_true")
    ap.add_argument("--skip-postgres", action="store_true")
    ap.add_argument("--skip-redis", action="store_true")
    ap.add_argument(
        "--settle",
        type=float,
        default=harness.SETTLE_S,
        help="idle seconds before the load reading that decides contention (#160)",
    )
    args = ap.parse_args()
    res = bench(
        args.profile,
        args.api,
        repeats=args.repeats,
        cold=args.cold,
        skip_parallel=args.skip_parallel,
        skip_postgres=args.skip_postgres,
        skip_redis=args.skip_redis,
        settle_s=args.settle,
    )
    outdir = ROOT / "results"
    outdir.mkdir(exist_ok=True)
    tag = "-contended" if res["contended"] else ""
    path = outdir / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{args.profile}{tag}.json"
    path.write_text(json.dumps(res, indent=2, default=str))
    print(json.dumps(res, indent=2, default=str))
    print(
        f"\nwritten {path}" + (" (CONTENDED: not for publication)" if tag else ""), file=sys.stderr
    )


if __name__ == "__main__":
    main()
