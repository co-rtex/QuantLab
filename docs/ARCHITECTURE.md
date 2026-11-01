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
* Production settings refuse placeholder secrets, insecure cookies and an unprotected
  `/metrics` endpoint.
* No user or AI code is ever executed; strategies are selected from a registry and
  parameterized through validated schemas.

## Observability

Prometheus scrapes two kinds of process, both behind the same bearer token
(`QUANTLAB_METRICS_TOKEN`, required in production):
* **The API**, at `api:8000/metrics`: HTTP latency by route template and DB statement
  latency, aggregated over its uvicorn processes.
* **Each worker replica**, at `<replica>:9101/metrics` (#165): queue wait, job run time and
  outcomes, retries, running jobs, cache hit/miss, latest dataset rows and freshness,
  ingestion quality.
  * Prometheus finds the replicas through DNS on the `worker` service name. The port is
    never published.
  * Jobs run in RQ's forked work horses, so each worker container keeps its own Prometheus
    multiprocess directory, on a tmpfs, and its worker process serves that directory.
  * The directory is never shared with another container, where every worker is pid 1 and
    the per-pid files would collide, or with the API.
  * After each horse exits, its live gauges are removed (`mark_process_dead`), so a horse
    killed mid-job doesn't leave `quantlab_jobs_running` raised.
  * Its other files are folded into one archive file per kind
    (`observability.compact_dead_process`). Counters and histograms are added, so they
    never go down, and scrapes wait while a fold runs. Without this, every job (a new pid)
    left three files and about 20 KiB of tmpfs, which counts against the worker's memory
    limit, and every scrape read them all: 466 ms at 5000 jobs. With it, the directory
    holds five files and a scrape takes about 0.4 ms however many jobs have run. Structured JSON logs (structlog) carry
`request_id`, and job logs carry the originating request id.
