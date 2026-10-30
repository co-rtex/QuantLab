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

The roadmap (validation suite, Research Integrity Report, ML, grounded assistant, reports,
benchmarks) is tracked in [ROADMAP.md](ROADMAP.md); current state is in [STATUS.md](STATUS.md).

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

Requirements: Docker, Python 3.12 with [uv](https://docs.astral.sh/uv/), Node.js 22.

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
