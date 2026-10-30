# Status (authoritative handoff)

_Last updated: 2026-10-30_

## Where things stand
All roadmap phases 0–10 are implemented and verified for the documented scope. The only
open items are Terraform (not started), a hosted-LLM evaluation (blocked on credentials)
and load testing from a dedicated host. Details are in [ROADMAP.md](ROADMAP.md).

| Area | State |
|---|---|
| Market data | Synthetic and CSV providers, NYSE calendar, PIT adjustment, quality checks, content-addressed Parquet versions, DuckDB + Redis retrieval, resumable ingestion |
| Simulation | Event-driven engine v1.1.0 (next-session fills, costs, pro-rata partial fills, PIT delisting), 9 strategies, ledger with replay invariants |
| Analytics | Performance (undefined-aware), VaR/ES, drawdowns, attribution, factor exposures (Newey-West), Monte Carlo, stress, optimizers |
| Research | Walk-forward, sensitivity, cost sweeps, bootstrap CIs, PSR/DSR, regimes, engine-level leakage check, Research Integrity Report, ML pipeline with purged CV |
| Product | Auth, workspaces and roles, experiments, comparison, reproduction, portfolios, construction, ML, grounded assistant, HTML/Markdown reports |
| Platform | Durable Postgres-backed jobs (fencing, retries, cancel, timeouts, reaper, keepalive, per-workspace cap), OpenTelemetry request→job traces, Prometheus/Grafana, runbook |
| Quality | Two independent reviews (financial core, service layer) with every finding fixed and regression-tested; axe WCAG 2.1 AA audit clean on 12 pages |

## Last verified (2026-10-30, commit `04a0804` and later)
* Python: 154 tests passing (unit, property-based, golden, research, ML, assistant evals,
  integration, security, migrations, failure injection, tracing). ruff and mypy --strict
  are clean.
* Web: eslint, tsc and next build pass. Playwright 12/12 (workflow, portfolios and
  construction, accessibility, auth and proxy security), run against the containerized
  stack from `make app-up`.
* CI: python, web and docker jobs green on recent commits.
* Measurements: docs/BENCHMARKS.md (10.4M-row profile, engine scaling, retrieval, cache,
  API load, worker scaling) and docs/AI_EVAL.md.
* Acceptance case study: docs/CASE_STUDY.md, executed through the API and reproduced by a
  second researcher.

## Next steps (highest value first)
1. Run `quantlab.assistant.evals` against `AnthropicProvider` once `QUANTLAB_ANTHROPIC_API_KEY` exists; record results in docs/AI_EVAL.md.
2. Load test from a separate machine to find the API's real ceiling (current numbers are limited by the co-located load generator).
3. Terraform for a cloud deployment (managed Postgres, Redis, object storage for `QUANTLAB_DATA_DIR`, container service), validated but not applied.
4. Real-data ingestion via a licensed provider adapter (the CSV importer covers user-supplied data today).

## Blockers
No LLM API key, market-data API key or cloud credentials (docs/BLOCKERS.md).

## Local run notes
* `make up` starts Postgres (5433) and Redis (6380). `make app-up` starts the full stack:
  web on :3000, API on :8000 (4 uvicorn workers), 2 workers and the reaper.
* Dev processes log to `logs/` (gitignored). On macOS the RQ worker needs
  `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` (set by `make worker`).
* After `next build`, kill any old `next start` on :3000 or chunks will 404.
* In development the JWT secret is random per API process, so restarting the API signs
  everyone out.
* Gate commits on `&&`-chained checks; piping `mypy` or `pytest` into `tail` hides failures.
