# Status (authoritative handoff)

_Last updated: 2026-10-30 (lead integration: #1 and #2 merged, coordination established)_

## Where things stand
All roadmap phases 0–10 are implemented. An independent audit on 2026-10-30
([docs/AUDIT_2026-10-30.md](docs/AUDIT_2026-10-30.md)) found and fixed 18 defects,
including a corporate-action bug that could fabricate a 50% loss, a login-throttling
lockout of all web users, a job-submission deadlock, undeletable workspaces, ML
survivorship bias and look-ahead tests that could not detect a one-bar leak. Its
prioritized backlog supersedes the old next-steps list and is now tracked as GitHub
issues #3-#25 (dependency order in [ROADMAP.md](ROADMAP.md)). Multi-agent ownership,
leases and migration slots: [docs/COORDINATION.md](docs/COORDINATION.md).

> **Security gate (P0, #3): do not expose any deployment publicly.** Without a TLS edge
> that overwrites `X-Forwarded-For`, a client reaching the web tier chooses the address
> the API rate-limits on (it can target a victim's bucket and spend a victim account's
> login-failure budget). Compose binds every port to 127.0.0.1; keep it that way until #3
> is closed and verified live.

| Area | State |
|---|---|
| Market data | Synthetic and CSV providers, NYSE calendar, PIT adjustment, quality checks, content-addressed Parquet versions, DuckDB + Redis retrieval, resumable ingestion |
| Simulation | Event-driven engine v1.1.0 (next-session fills, costs, pro-rata partial fills, PIT delisting), 9 strategies, ledger with replay invariants |
| Analytics | Performance (undefined-aware), VaR/ES, drawdowns, attribution, factor exposures (Newey-West), Monte Carlo, stress, optimizers |
| Research | Walk-forward, sensitivity, cost sweeps, bootstrap CIs, PSR/DSR, regimes, engine-level leakage check, Research Integrity Report, ML pipeline with purged CV |
| Product | Auth, workspaces and roles, experiments, comparison, reproduction, portfolios, construction, ML, grounded assistant, HTML/Markdown reports |
| Platform | Durable Postgres-backed jobs (fencing, retries, cancel, timeouts, reaper, keepalive, per-workspace cap), OpenTelemetry request→job traces, Prometheus/Grafana, runbook |
| Quality | Three independent reviews (financial core, service layer, full audit 2026-10-30) with every fixed finding regression-tested; axe WCAG 2.1 AA audit clean on 12 pages |

## Integrated on main
* #1 independent audit (18 fixes, per-worktree isolation), merge `656510d`.
* #2 account-wide quotas: `QUANTLAB_MAX_ACTIVE_JOBS_PER_USER` (default 50, effective cap
  is min with the per-workspace cap) and `QUANTLAB_MAX_WORKSPACES_PER_USER` (default 5,
  including the default workspace), both enforced under row locks. Merge `c4f5460`.
* Both had independent read-only reviews (financial core; security/service) with no
  blockers. Their should-fix findings are issues #3, #6, #8, #10, #13 and #15.

## Last verified (2026-10-30, #2 head `588a537`, tree identical to main `c4f5460`)
* CI on main and on both PRs: python (ruff, mypy --strict, pytest with Postgres), web
  (lint, typecheck, build) and docker are green.
* Local, isolated stack (slot 85): ruff, format and mypy are clean; **206 tests passed**.
  `tests/golden/` is empty; the golden cases live in `tests/unit` (#24).
* Playwright: **11/12** on a fresh stack. `workflow.spec.ts` (the regime panel after
  validation) fails only in full-suite runs, and fails identically on the pre-audit
  `1373f53`, so it is pre-existing (#16). E2E isn't in CI yet (#17).
* Earlier runs had the API OOM-killed: three full stacks in the 3.8 GiB Docker VM. Peak
  API memory is the same before and after the PRs (about 810 MiB), so it isn't a
  regression. See the memory budget in docs/COORDINATION.md.
* Measurements: docs/BENCHMARKS.md and docs/AI_EVAL.md (unchanged by the integration).

## Next steps (highest value first; issues carry owners and acceptance criteria)
1. Agent 2, P0: #3 trusted edge (security gate), #4 auth before multipart parsing,
   #5 invitations (migration slot M1). Then #6, #7, #8.
2. Agent 3, P0: #9 permanent instrument identity (migration slot M2 after M1),
   #10 corporate-action gates. Then #12 reconciliation and #11 vendor adapter (live part
   blocked on credentials). P1: #13, #14.
3. Lead: #16 E2E flake, #17 E2E in CI, #22 isolation hardening, #25 job-deletion
   semantics. Later: #19 Terraform (after #3).
4. Unassigned, queued: #15 assistant verifier and hosted eval, #18 frontend errors,
   #20 external load test, #21 storage lifecycle, #23 frontend completeness,
   #24 benchmarks.

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
