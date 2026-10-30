# Benchmarks

All figures below were measured with `benchmarks/run_all.py` on 2026-10-30. Raw results,
including library versions, are in `benchmarks/results/*.json`. Reproduce with:

```bash
make up
uv run python benchmarks/run_all.py --profile small --api http://localhost:8000
uv run python benchmarks/run_all.py --profile medium
uv run python benchmarks/run_all.py --profile large
```

**Hardware:** Apple Silicon laptop (arm64), 8 CPU cores, 8 GB RAM, macOS 26.6, Python 3.12.16,
NumPy 2.5.4, pandas 3.1.0, DuckDB 2.0.0, PyArrow 26.0.0; PostgreSQL 17 and Redis 7 in
Docker Desktop (4 GB VM). Data is synthetic (`quantlab.data.synthetic`, seed 1).

## Workloads

| Profile | Assets | Calendar years | Rows | Generate | Quality checks | Write Parquet |
|---|---|---|---|---|---|---|
| small | 100 | 6 | 142,384 | 0.21 s | 0.18 s | 0.13 s |
| medium | 500 | 6 | 711,358 | 0.81 s | 0.81 s | 0.27 s |
| large | 1,000 | 10 | 2,357,984 | 3.01 s | 2.61 s | 0.91 s |

## Backtest engine (5 trading years, 1,258 sessions, monthly rebalance, next-open fills)

| Universe | Equal weight | Momentum (top 10) |
|---|---|---|
| 100 assets | 0.28–0.35 s | 0.17–0.23 s |
| 500 assets | 1.28 s | 0.54 s |
| 1,000 assets | 3.22 s (41,520 fills) | 1.95 s |

Target: a 100-asset, five-year daily backtest in under 60 s. Measured: under 0.4 s. The
engine scales roughly linearly with universe size; equal weight is slower because it
trades every asset each month.

## Parallel independent backtests (8 runs, warmed process pool)

| Profile | Serial | 2 workers | 4 workers | Speedup (4) |
|---|---|---|---|---|
| small (100 assets) | 1.38 s | 1.45 s | 1.10 s | 1.25× |
| medium (500 assets) | 4.49 s | 3.67 s | 3.03 s | 1.48× |
| large (1,000 assets) | 17.74 s | 12.51 s | 14.87 s | 1.19× |

Scaling is weak on this machine. Small runs (~0.16 s each) are dominated by per-task
overhead (each task reads the Parquet file and builds wide matrices). Large runs keep about
1.3 GB per process, and four processes exceed what an 8 GB laptop running Docker can hold
comfortably, so they contend for memory. The serial engine is fast enough for the target
workloads. To scale out, add worker replicas on hosts with memory headroom (compose sets
1.5 GB per worker) rather than oversubscribing one host. Not yet measured: RQ worker
scaling across separate containers.

## Historical retrieval: one asset, five years

| Method | small p95 | medium p95 | large p95 |
|---|---|---|---|
| DuckDB over Parquet, 1M-row groups (initial) | 7.1 ms | 11.2 ms | 12.6 ms |
| DuckDB over Parquet, 50k-row groups (current) | | | **4.4 ms** (p50 3.4 ms) |
| PyArrow dataset filters, 50k-row groups | | | 12.8 ms |
| PostgreSQL rows, no index | 14.2 ms | 51.6 ms | 48.5 ms |
| PostgreSQL rows, (symbol, date) B-tree | 3.1 ms | 3.3 ms | 3.4 ms |
| API path, adjusted full history, Redis miss | 30.3 ms | 43.1 ms | 30.5 ms |
| API path, adjusted full history, Redis hit | 4.4 ms | 4.9 ms | 3.5 ms |

Target: p95 under 200 ms for one asset over five years. Measured: under 50 ms uncached and
under 5 ms cached.

**Optimization applied.** The default PyArrow row group (1M rows) left the large file with
three row groups, so DuckDB could barely prune. Writing 50k-row groups (rows are sorted by
symbol) cut single-symbol p50 from 11.9 ms to 3.5 ms (3.4×) for a 66% larger file
(56.8 → 94.4 MB). Content hashes cover the data, not the file layout, so version identity
is unchanged. An indexed Postgres row table is faster still for point lookups but costs a
7.6 s load and much more space per row. Parquet stays the system of record because
versions must be immutable files that DuckDB can also scan for analytics.

## API latency (small profile, 200 sequential requests each, local uvicorn, 1 process)

| Endpoint | p50 | p95 | p99 |
|---|---|---|---|
| `GET /health` | 0.64 ms | 0.74 ms | 0.86 ms |
| `GET /workspaces` (auth + DB) | 2.42 ms | 4.33 ms | 5.82 ms |
| `GET /workspaces/{id}/experiments` | 4.61 ms | 7.35 ms | 8.71 ms |
| `GET /strategies/catalog` | 1.67 ms | 2.20 ms | 2.94 ms |

Target: lightweight reads p95 under 300 ms (warm under 100 ms). Measured: under 8 ms.
These numbers come from sequential requests; concurrent load testing is not yet done.

## Concurrent HTTP load (containerized stack, 4 uvicorn workers)

`benchmarks/load_and_scaling.py load`, run from inside the API container so Docker
Desktop's port forwarding is not measured. Each level sends max(200, 20 × concurrency)
requests. There were no errors at any level.

| Endpoint | Concurrency | Throughput | p50 | p95 |
|---|---|---|---|---|
| experiments list (auth + DB) | 1 | 329 req/s | 2.4 ms | 5.3 ms |
| | 8 | 778 req/s | 7.1 ms | 23.5 ms |
| | 32 | 453 req/s | 46.9 ms | 191 ms |
| | 64 | 271 req/s | 162 ms | 658 ms |
| bars, adjusted, Redis hit | 8 | 635 req/s | 6.7 ms | 27.5 ms |
| | 32 | 459 req/s | 43.9 ms | 163 ms |
| health | 8 | 1,538 req/s | 4.8 ms | 6.0 ms |
| | 64 | 319 req/s | 137 ms | 582 ms |

Lightweight reads meet the p95 < 300 ms target up to 32 concurrent clients. Beyond that,
even `/health`, which does no work, slows down. That is the signature of the single
Python load-generator process competing with the API for the same 8-CPU Docker VM, so
these figures understate the API's capacity. A load generator on separate hardware is
needed to find the true ceiling. Running 4 uvicorn workers required Prometheus
multiprocess mode, which compose now configures.

## Worker scaling (24 momentum backtests, 200 assets, 2018–2023)

`benchmarks/load_and_scaling.py scaling --workers 1 2 4 --jobs 24` scales the compose
`worker` service. Wall time runs from the first submission to the last completion.

| Worker containers | Wall time | Throughput | Speedup |
|---|---|---|---|
| 1 | 18.4 s | 78 jobs/min | 1.00× |
| 2 | 14.6 s | 99 jobs/min | 1.26× |
| 4 | 11.5 s | 126 jobs/min | 1.61× |

All 24 jobs completed in each configuration. Scaling is sublinear because the workers,
API, PostgreSQL and Redis share one 8-CPU VM, and each job loads the dataset from Parquet
before simulating (about 0.4 s of the roughly 0.75 s per job).

## Not yet measured
The 10M-observation `xl` profile (the harness supports `--profile xl`), load from a
dedicated generator host, and failure-recovery timing.
