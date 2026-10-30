# Status (authoritative handoff)

_Last updated: 2026-10-30 (audit)_

## Where things stand
All roadmap phases 0–10 are implemented. An independent audit on 2026-10-30
([docs/AUDIT_2026-10-30.md](docs/AUDIT_2026-10-30.md)) found and fixed 18 defects,
including a corporate-action bug that could fabricate a 50% loss, a login-throttling
lockout of all web users, a job-submission deadlock, undeletable workspaces, ML
survivorship bias and look-ahead tests that could not detect a one-bar leak. Its
prioritized backlog supersedes the old next-steps list. Details in [ROADMAP.md](ROADMAP.md).

| Area | State |
|---|---|
| Market data | Synthetic and CSV providers, NYSE calendar, PIT adjustment, quality checks, content-addressed Parquet versions, DuckDB + Redis retrieval, resumable ingestion |
| Simulation | Event-driven engine v1.1.0 (next-session fills, costs, pro-rata partial fills, PIT delisting), 9 strategies, ledger with replay invariants |
| Analytics | Performance (undefined-aware), VaR/ES, drawdowns, attribution, factor exposures (Newey-West), Monte Carlo, stress, optimizers |
| Research | Walk-forward, sensitivity, cost sweeps, bootstrap CIs, PSR/DSR, regimes, engine-level leakage check, Research Integrity Report, ML pipeline with purged CV |
| Product | Auth, workspaces and roles, experiments, comparison, reproduction, portfolios, construction, ML, grounded assistant, HTML/Markdown reports |
| Platform | Durable Postgres-backed jobs (fencing, retries, cancel, timeouts, reaper, keepalive, per-workspace cap), OpenTelemetry request→job traces, Prometheus/Grafana, runbook |
| Quality | Three independent reviews (financial core, service layer, full audit 2026-10-30) with every fixed finding regression-tested; axe WCAG 2.1 AA audit clean on 12 pages |

## P0 implementation in review (branch `fix/p0-account-quotas`)
* Added configurable account-wide active-job and owned-workspace limits, enforced under
  database row locks; the existing per-workspace limit remains in effect.
* Added integration regressions for cross-workspace job limits, idempotent retries,
  cancellation releasing capacity, and concurrent job/workspace requests.
* **Not yet independently verified:** run Python lint, mypy, all tests and CI before
  merging this branch into `audit/deep-verification` or deploying.

## Last verified (2026-10-30, branch `audit/deep-verification`)
* Python: 202 tests passing (unit, property-based, research, ML, assistant evals,
  integration, concurrency, security, migrations, failure injection, tracing). ruff and
  mypy --strict are clean. `tests/golden/` is empty; the golden cases live in `tests/unit`.
* Web: eslint and tsc pass. Playwright 12/12 against a per-worktree stack from `make app-up`.
* Live checks on an isolated compose stack: auth across the 4 API workers (issue, accept,
  revoke, tamper), proxy client addresses, report CSP, 413 body cap, full workflow
  through the proxy including workspace deletion.
* Measurements: docs/BENCHMARKS.md (load table regenerated from committed results, with
  its limits stated) and docs/AI_EVAL.md.

## Next steps (highest value first; full list in docs/AUDIT_2026-10-30.md)
1. P0: per-user job cap / workspace quota; authenticate before parsing multipart uploads;
   invitation flow instead of add-member by email.
2. P0: real financial-data ingestion through a licensed adapter, with survivorship-free
   universes and corporate actions gated by quality checks.
3. P1: hosted-LLM evaluation with an independent judge; Terraform (validated, not
   applied); external load testing with seeded data and repeats.

## Blockers
No LLM API key, market-data API key or cloud credentials (docs/BLOCKERS.md).

## Local run notes
* `make up` starts Postgres (5433) and Redis (6380). `make app-up` starts the full stack:
  web on :3000, API on :8000 (4 uvicorn workers), 2 workers and the reaper. In a linked
  git worktree the Makefile uses a separate compose project and port block
  (`make stack-env`); see README.
* Dev processes log to `logs/` (gitignored). On macOS the RQ worker needs
  `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` (set by `make worker`).
* After `next build`, kill any old `next start` on :3000 or chunks will 404.
* In development an unset JWT secret is generated once into `$QUANTLAB_DATA_DIR/.jwt-dev-secret`
  and shared by all API processes; delete the file to sign everyone out.
* Gate commits on `&&`-chained checks; piping `mypy` or `pytest` into `tail` hides failures.
