# QuantLab

QuantLab is a reproducible quantitative research platform. You can ingest and version
historical market data, define strategies, run event-driven backtests as background jobs,
measure performance and risk, and reproduce any experiment exactly from its manifest.

It is research infrastructure, not a trading bot. Results describe the past under stated
assumptions and are never investment advice.

## What works today

| Area | Capability |
|---|---|
| Market data | NYSE trading calendar, raw OHLCV with availability timestamps, splits and dividends, point-in-time total-return adjustment, 10 quality checks, content-addressed immutable Parquet versions, DuckDB retrieval with Redis caching, resumable throttled ingestion with backoff |
| Accounting | Transaction ledger with replay audit, value conservation, corporate actions, rejection of impossible trades |
| Backtesting | Daily event-driven engine with next-session fills, slippage, commissions, partial fills, rejections, delisting liquidation and deterministic fingerprints. Nine strategies: buy and hold, equal weight, SMA crossover, momentum, mean reversion, low volatility, minimum variance, risk parity, volatility target |
| Analytics | Return, risk-adjusted, benchmark-relative and drawdown statistics with explicit undefined handling. Historical, Gaussian and Cornish-Fisher VaR/ES, risk contributions, stress scenarios, bootstrap Monte Carlo, FIFO trade statistics, cost attribution |
| Reproducibility | Input hash, engine version, code version, dataset hash and output fingerprint for every experiment; one-click reproduction with automatic fingerprint comparison; downloadable manifest |
| Platform | FastAPI with OpenAPI docs, Argon2id auth, JWT plus httpOnly cookie sessions, workspace roles, audit log, durable Postgres-backed jobs (leases, fencing, retries, cancellation, timeouts, reaper), Prometheus metrics, structured logs with request correlation |
| Frontend | Next.js research workspace: market-data explorer, quality report, strategy builder, live job progress, results explorer (equity and drawdown, benchmark table, tail risk, attribution, monthly returns, trade blotter, holdings by date), comparison, workspace administration |

| Validation | Walk-forward with in-sample parameter selection, parameter and cost sweeps, bootstrap confidence intervals, deflated Sharpe, regime breakdown, engine-level look-ahead check, and a Research Integrity Report |
| ML research | Point-in-time features, purged and embargoed chronological CV, baselines against logistic regression, random forest and gradient boosting |
| Assistant and reports | Evidence-grounded assistant with verified citations and a deterministic fallback ([eval](docs/AI_EVAL.md)); HTML and Markdown report export |

See the [case study](docs/CASE_STUDY.md) for the whole workflow on one research question,
[benchmarks](docs/BENCHMARKS.md) for measured performance, [ROADMAP.md](ROADMAP.md) for what
remains, and [STATUS.md](STATUS.md) for the current state.

## Architecture

```
 Browser ──► Next.js (apps/web) ──/api/*──► FastAPI (services/api) ──► PostgreSQL
                                              │        │                (metadata, jobs,
                                              │        └─ enqueue ─► Redis/RQ   results)
                                              │                         │
                                              └─ DuckDB ◄── Parquet ◄── Workers (RQ)
                                                  (dataset versions,    run quantlab domain
                                                   experiment artifacts) library
```

* `packages/quantlab`: pure domain library (no web or DB imports).
* `services/api`: HTTP API, persistence, job system, worker and CLI entrypoints.
* `apps/web`: Next.js 16 frontend that talks to the API through a same-origin proxy.

Details are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), the formulas in
[docs/METHODOLOGY.md](docs/METHODOLOGY.md), and the decisions in [DECISIONS.md](DECISIONS.md).

## Quick start

Requirements: Docker, Python 3.12 with [uv](https://docs.astral.sh/uv/), Node.js 22. The
full stack (`make app-up`, 2 workers) needs at least **4.5 GiB of Docker VM memory**: that's
the measured peak of 3,685 MiB with two backtests at the admission cap, plus 20% (#175;
[RUNBOOK, Resources](docs/RUNBOOK.md#resources)). `make` warns on smaller VMs.

```bash
make setup            # uv sync + npm ci
make up               # PostgreSQL 17 and Redis 7 (ports 5433, 6380 on localhost)
make api-dev          # migrations + API on :8000   (terminal 1)
make worker           # background worker            (terminal 2)
make reaper           # stale-job recovery           (terminal 3)
make web-dev          # frontend on :3000            (terminal 4)
```

Or run everything in containers: `make app-up` (web on :3000, API on :8000, two workers).

Open http://localhost:3000, create an account, generate a synthetic dataset under
**Market data**, then create an experiment. API docs: http://localhost:8000/api/v1/docs.

### Several git worktrees at once

The ports above are the main checkout's. In a linked worktree, `make` gives the checkout
its own Compose project, volumes and port block (`make stack-env` prints them; override
with `QUANTLAB_SLOT=<1-89>`), so `make up`, `make app-up`, `make api-dev`, `make web-dev`
and `make e2e` never touch another checkout's containers. Integration tests use a
per-worktree database (`quantlab_test_<hash>`) and a session advisory lock, so concurrent
runs that do share a database wait for each other instead of truncating each other's tables.

## Testing

```bash
make lint typecheck   # ruff + mypy --strict
make test-unit        # domain tests (golden, property-based, look-ahead checks)
make test-integration # API, security, migrations, job failure injection (needs `make up`)
make coverage
make e2e              # Playwright, against a running stack
```

See [docs/TEST_MATRIX.md](docs/TEST_MATRIX.md) for what each layer covers.

## Data and limitations

No licensed market-data feed or LLM credentials are configured in this environment. All
bundled data is **synthetic** and labelled as such in the API (`is_synthetic`) and in the
UI. A CSV provider is available for user-supplied data whose licence permits local
research use. See [docs/METHODOLOGY.md](docs/METHODOLOGY.md#known-limitations) and
[docs/BLOCKERS.md](docs/BLOCKERS.md).
