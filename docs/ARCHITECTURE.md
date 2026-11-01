# Architecture

## Components

| Component | Tech | Responsibility |
|---|---|---|
| `packages/quantlab` | NumPy, pandas, SciPy, scikit-learn, PyArrow | Domain: calendar, schema, adjustment, quality, providers, store, ledger, strategies, engine, analytics, optimizers. No I/O to databases or HTTP. |
| `services/api` (`quantlab_api`) | FastAPI, SQLAlchemy 2, Alembic, psycopg 3, RQ, DuckDB | HTTP API, auth, authorization, persistence, job orchestration, artifacts, metrics |
| Worker | RQ worker (`quantlab-worker`) | Executes job handlers in forked work horses |
| Reaper | `quantlab-cli reaper` | Recovers expired leases, redelivers lost queue messages |
| `apps/web` | Next.js 16, React 19, Tailwind 4, Recharts | Research workspace UI; `/api/*` same-origin proxy keeps the session cookie first-party |
| PostgreSQL 17 | | Users, workspaces, catalogs, experiments, job state, results, audit |
| Redis 7 | | Job delivery (RQ), bar-query cache, shared login throttling |
| Parquet files | | Immutable dataset versions and experiment artifacts (`QUANTLAB_DATA_DIR`) |

## Module boundaries

```
quantlab.data ──► quantlab.strategies ──► quantlab.backtest ──► quantlab.analytics
      │                    ▲                     │
      └──► quantlab.portfolio (ledger) ◄─────────┘
quantlab.optimize ◄── strategies (min-variance, risk parity), analytics (risk contributions)

quantlab_api.routes ──► quantlab_api.jobs ──► quantlab_api.tasks ──► quantlab (domain)
        │                     │                       │
        └── security, schemas └── models, db          └── artifacts, marketdata
```

Dependencies point inward: the domain never imports the service; routes never run
simulations (they submit jobs).

## Request → job → result

1. `POST /api/v1/workspaces/{ws}/experiments` validates strategy params (Pydantic),
   config (`BacktestConfig`) and symbols against the dataset catalog, computes the input
   hash, inserts the experiment and a `jobs` row (idempotent per `Idempotency-Key`),
   commits, then enqueues the job id in RQ. The request id is stored on the job.
2. A worker claims the job with an atomic conditional update (attempt = fencing token),
   loads the exact dataset version, runs the engine with progress heartbeats that extend
   the lease and observe cancellation/timeouts.
3. Artifacts are written to a staging directory; the finalizer, inside the commit
   transaction, re-checks the fencing token, publishes the directory, inserts the result
   (unique per experiment) and marks the job completed.
4. The UI polls the job and experiment and then reads results from the artifact
   endpoints. Logs for the HTTP request and the job share the request id.

## Data layout

```
$QUANTLAB_DATA_DIR/
  datasets/<slug>/<sha256>/{bars,actions,instruments}.parquet, manifest.json
  artifacts/experiments/<experiment-id>/{equity,trades,positions,weights,rejections}.parquet, analysis.json
  artifacts/staging/<experiment-id>-<attempt>/   (transient)
```

## Security model

* Argon2id password hashes; JWT (HS256) with issuer, expiry and a per-user token version
  for revocation; session in an httpOnly SameSite=Lax cookie or a bearer header.
* Authorization at every data-access boundary through workspace membership
  (`require_workspace`); non-members get 404. Roles: viewer < editor < owner.
* Login throttling per client address and per account (failures only), shared via Redis.
* Production settings refuse placeholder secrets, insecure cookies, a Redis URL without a
  real password, and an unprotected `/metrics` endpoint.
* Every published port binds to 127.0.0.1. A deployment is reached only through the TLS edge
  (`infra/edge`, ADR-0008), and public exposure is prohibited until #39 is closed
  (docs/RUNBOOK.md, "Exposing a deployment").
* No user or AI code is ever executed; strategies are selected from a registry and
  parameterized through validated schemas.

## Observability

Prometheus scrapes three kinds of process, all behind the same bearer token
(`QUANTLAB_METRICS_TOKEN`, required in production by each process that serves metrics):
* **The API**, at `api:8000/metrics`: HTTP latency by route template and DB statement
  latency, aggregated over its uvicorn processes. Also
  `quantlab_rate_limiter_breaker_open{limiter="login"}` (#207, `livemax`): 1 while any
  live API process's rate limiter counts in-process after a Redis error, still limiting.
  The API never marks a dead uvicorn process, so a process that died with its breaker
  open leaves 1 until the container restarts. Also
  `quantlab_heavy_requests_refused_total{route}` (#223): heavy requests refused with
  `503 server_busy`, `route` being `portfolio_create`, `portfolio_transaction`
  or `portfolio_valuation`. And `quantlab_cache_events_total{cache,result}`: the bars
  cache's `hit`, `miss` and `error`.
* **Each worker replica**, at `<replica>:9101/metrics` (#165): queue wait, job run time and
  outcomes, retries, running jobs, DB statement latency, latest dataset rows and freshness,
  ingestion quality.
  * A worker serves only when `PROMETHEUS_MULTIPROC_DIR` is set (compose sets it); without
    it the job metrics are lost with each horse, and the worker needs no token.
  * Prometheus finds the replicas through DNS on the `worker` service name. The port is
    never published.
  * Jobs run in RQ's forked work horses, so each worker container keeps its own Prometheus
    multiprocess directory, on a tmpfs, and its worker process serves that directory.
  * The directory is never shared with another container, where every worker is pid 1 and
    the per-pid files would collide, or with the API.
  * After each horse exits, its live gauges are removed (`mark_process_dead`), so a horse
    killed mid-job doesn't leave `quantlab_jobs_running` raised.
  * Its other files are folded into one archive file per kind
    (`observability.compact_dead_process`), all or nothing. Counters and histograms are
    added, so they never go down, and scrapes wait while a fold runs. The collector adds
    files in a fixed order (archives, then by pid), so a fold changes no scraped value,
    not even the last bit of a float sum.
  * The directory holds the worker process's own two files and three archives, plus the
    running job's files while one runs, however many jobs have run.
  * Retries here are the ones a worker schedules after a retryable error. Retries after a
    lost worker are the reaper's (below).
  * Measured on 2026-11-01 in the worker image on a real tmpfs, on an 8-CPU machine with a
    3.83 GiB Docker VM, at load average about 2.5 (`benchmarks/worker_metric_files.py`;
    `benchmarks/results/20261101T043624Z-worker-metric-files.log`). Without the fold,
    every job left three files and about 20 KiB of tmpfs, which counts against the
    worker's memory limit. Every scrape read them all: 483 ms at 5,000 jobs. With it, a
    scrape takes about 0.4 ms at any count, and a fold about 0.23 ms.
* **The reaper**, at `reaper:9102/metrics` (#179): retries after a lost worker
  (`quantlab_job_retries_total{reason="lease_expired"}`), the outcomes it decides (cancelled,
  timed out, out of attempts, and cancelled before any worker saw it), and its DB statement
  latency.
  * It serves only when `QUANTLAB_REAPER_METRICS_PORT` is set (compose sets 9102). Without
    the port it serves nothing and needs no token, like the Terraform ECS task.
  * It is one process that never forks, so its samples are in its own registry, with no
    multiprocess directory. A restart starts its counters from zero, which Prometheus reads
    as a reset.
  * It counts a decision only once it is committed, so a failed commit counts nothing and
    the next pass counts it once.
  * If its metrics port can't be bound, the reaper stops at start, before its first
    pass. Compose restarts it under the default restart policy (`unless-stopped`; a
    worktree slot's `no` leaves it stopped).
* **Each job's terminal outcome is counted once**, by whichever process's write wins the
  job's row: the worker (`completed`, `failed`, `timed_out`, `cancelled`), or the reaper
  when it decides first.
  * A worker whose write is fenced, because the reaper already decided the job after its
    lease ran out, counts `quantlab_job_outcomes_total{outcome="lease_lost"}` instead.
  * `lease_lost` and `retry` are not terminal outcomes. A sum of terminal outcomes, for
    example jobs finished per hour, must exclude both.

Structured JSON logs (structlog) carry `request_id`, and job logs carry the originating
request id.
