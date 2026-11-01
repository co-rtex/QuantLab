# Benchmarks

**These are measurements on one shared developer Mac, not a capacity claim.** They show
how the code behaves on that machine on one afternoon. They say nothing about what a
server, a cloud deployment or a different laptop would do, and no figure here is
extrapolated.

## Conditions (2026-10-31, quiet window for #24)

The runs were taken during a quiet window the user approved: the other agents stopped
their stacks and ran nothing locally. The rules are in `benchmarks/README.md`: only
results that are `contended: false` at both ends (1-minute load at or below half the
cores) and `git_dirty: false` are published. Each published figure is copied from the raw
JSON in `benchmarks/results/` without further rounding.

**Code measured:** `da8767a` (each result's `environment.git_sha`), engine 1.4.0. The engine
has changed since, and these figures were not re-timed:
* #163's column store cut engine memory by 23–26%, with results bit-for-bit identical in
  an independent 24-scenario A/B (docs/RELEASE_READINESS.md);
* engines 1.4.1 to 1.6.0 changed rejections, pro-rata and noise orders, and add work per
  fill (the exact pro-rata factor, the overdraw cut).

| | |
|---|---|
| Machine | Apple M3 (arm64), 8 cores (`cpu_count`), 8.0 GB RAM (`memory_gb`) |
| OS | macOS 26.6.2 (`platform`: `macOS-26.6.2-arm64-arm-64bit`) |
| Docker VM | 3.83 GB memory (`docker_vm_memory_gb`), 8 CPUs (from `docker info`, not recorded in the JSON) |
| Software | Python 3.12.16, NumPy 2.5.4, pandas 3.1.0, DuckDB 2.0.0, PyArrow 26.0.0 |
| Code | git `da8767a` (`git_dirty: false`) |
| Cache | **warm**. Cold mode needs `sudo -n purge`, which was not available; the harness labelled the runs `warm (not requested)` |
| Data | **synthetic** (`quantlab.data.synthetic`, `data_is_synthetic: true`) |
| Services | PostgreSQL 17 and Redis 7 of this worktree's compose stack (slot 31) |
| Repeats | 5 per timed quantity (the default). N below is the number of samples behind each median |

The CPU model comes from `sysctl`; the JSON records the platform, core count and memory.
macOS background services alone keep the 1-minute load at about 1.

Reproduce (in a worktree, with its stack's ports from `scripts/stack-env.sh`):

```bash
QUANTLAB_BENCH_PG=postgresql://quantlab:quantlab@localhost:<pg port>/quantlab \
QUANTLAB_REDIS_URL=redis://localhost:<redis port>/1 \
uv run python benchmarks/run_all.py --profile small   # then --profile medium
```

## Published results

| Profile | File | Load at start / end (1-minute) |
|---|---|---|
| small (100 assets, 6 calendar years) | `benchmarks/results/20261031T211545Z-small.json` | 1.92 / 2.96 |
| medium (500 assets, 6 calendar years) | `benchmarks/results/20261031T211806Z-medium.json` | 3.34 / 3.51 |

Medians with the interquartile range (IQR) in brackets. "Single" means timed once.

### Data preparation

| Step | small | medium |
|---|---|---|
| Rows generated (symbols) | 142,384 (101) | 711,358 (501) |
| Generate (single) | 0.247 s | 0.877 s |
| Quality checks (N = 5) | 161.629 ms [6.179] | 778.982 ms [9.229] |
| Write Parquet (N = 5) | 75.366 ms [0.436] | 356.947 ms [19.727] |
| Parquet size | 5.77 MB | 28.73 MB |
| Quality status | `pass` | `pass` |

### Backtest engine (in process; 1,258 sessions, monthly rebalance, next-open fills; N = 5)

| Universe | Strategy | small run | medium run | Fills (small / medium) |
|---|---|---|---|---|
| 100 assets | equal weight | 302.5 ms [8.632] | 321.732 ms [16.0] | 5,450 / 5,468 |
| 100 assets | momentum | 183.301 ms [0.484] | 190.729 ms [1.687] | 755 / 772 |
| 500 assets | equal weight | n/a | 1476.717 ms [55.756] | 23,408 |
| 500 assets | momentum | n/a | 609.686 ms [29.546] | 789 |

The 100-asset rows were measured in both runs, on different synthetic universes (each
profile generates its own market), so their fill counts differ.

Peak resident memory of the engine section: 263.2 MB (small), 665.9 MB (medium).

### Parallel independent backtests (8 runs per repeat, warmed process pools; N = 5)

| | small | medium |
|---|---|---|
| Serial, 8 runs | 1466.114 ms [6.113] | 5138.827 ms [200.389] |
| One run (N = 40) | 176.855 ms [5.402] | 612.818 ms [34.947] |
| 2 workers, 8 runs | 791.127 ms [27.254] | 2846.181 ms [36.111] |
| 4 workers, 8 runs | 425.287 ms [48.812] | 2157.075 ms [137.671] |
| Speedup, 4 workers (median) | 3.45× | 2.38× |

The worker rows are not in process (`in_process: false`).

### Historical retrieval: one symbol, last five trading years (warm cache)

Symbols are sampled without replacement, with a fresh seeded sample per repeat.

| Method | small | medium |
|---|---|---|
| DuckDB over Parquet | 2.679 ms [0.286], N = 500 | 2.865 ms [0.199], N = 1000 |
| PyArrow dataset filters | 2.026 ms [0.242], N = 125 | 2.337 ms [0.19], N = 250 |
| PostgreSQL rows, no index | 7.085 ms [0.754], N = 150 | 15.282 ms [3.687], N = 150 |
| PostgreSQL rows, (symbol, date) B-tree | 1.258 ms [0.211], N = 500 | 1.236 ms [0.326], N = 1000 |
| PostgreSQL COPY load (single) | 0.56 s | 2.42 s |
| PostgreSQL index build (single) | 0.06 s | 0.32 s |

The PostgreSQL rows are round trips to the Docker VM (`in_process: false`).

### Adjusted full history through `bars_payload` (in process, Redis in Docker)

The Redis cache is cleared before each repeat, so every miss is a real miss.

| | small | medium |
|---|---|---|
| Redis miss | 20.289 ms [2.27], N = 500 | 21.026 ms [1.825], N = 1000 |
| Redis hit | 0.941 ms [0.59], N = 500 | 0.919 ms [0.312], N = 1000 |

Peak resident memory of the whole run: 317.1 MB (small), 690.5 MB (medium).

## Not published from this machine

**large and xl.** The end-of-run load check counts the benchmark's own load, and the
large profile's work alone takes this 8-core M3 past 4, from a macOS baseline of about 1.
Every large run started below the threshold and ended above it:

| File (kept for diagnosis only; no figures quoted) | Run | 1-minute load, start → end |
|---|---|---|
| `20261031T212632Z-large-contended.json` | full | 3.79 → 5.09 |
| `20261031T213553Z-large-contended.json` | full, rerun | 2.77 → 5.82 |
| `20261031T214034Z-large-contended.json` | `--skip-parallel` | 2.98 → 4.76 |

- The two full runs ended contended, partly because of their own 4-process parallel
  step on 8 cores. So large's parallel section is not published.
- The `--skip-parallel` run, labelled "large (--skip-parallel): parallel section not
  measured", ended contended as well. So nothing from large is published.
- xl is larger than large and was not run.

**HTTP load and worker scaling.** These ran with `benchmarks/load_and_scaling.py` at git
`00d90b9`, against this worktree's freshly started stack. The load generator ran on the
host and reached the API through Docker Desktop's port forwarding. All four runs ended
contended: the generator, the API, PostgreSQL, Redis and up to four workers share this one
machine. They are kept for diagnosis only, and no figures are quoted from them:

| File | Mode | 1-minute load, start → end |
|---|---|---|
| `20261031T214942Z-load-contended.json` | load | 2.77 → 4.49 |
| `20261031T215322Z-load-contended.json` | load, rerun | 2.82 → 4.59 |
| `20261031T215853Z-scaling-contended.json` | scaling 1/2/4 workers | 3.47 → 7.17 |
| `20261031T220408Z-scaling-contended.json` | scaling, rerun | 6.14 → 5.36 |

The rule was not changed for this window. Since #160, the harness decides contention
from the load at the start and after the run has settled for 180 idle seconds, not from
the reading straight at the end (`benchmarks/README.md`). The files above were judged by
the end-of-run rule, are not reclassified, and stay unpublished. large, xl, load and
scaling can be published only from new runs under the new rule.

## Earlier figures withdrawn

Earlier versions of this page quoted figures from the 2026-10-30 runs
(`benchmarks/results/20261030T*.json`), including the large, xl, load and scaling tables
and the Parquet row-group comparison. Those files predate the harness's variance, load and
tree-state metadata (#110): they record no repeats, no `contended` flag and no
`git_dirty`. So they cannot meet the publication rule, and their figures are no longer
quoted here. The files stay in the repository as history.

Two decisions those runs informed still stand. Parquet versions are written with
50k-row groups so DuckDB can prune by symbol. Parquet stays the system of record, because
dataset versions must be immutable files that DuckDB can also scan for analytics.

## Not yet measured

- Cold-cache reads (they need `sudo -n purge` on macOS or root on Linux).
- large and xl under the publication rule (now with the settled reading, #160).
- HTTP load from a separate generator host.
- Worker scaling on hosts with headroom.
- Failure-recovery timing.
