# Status (authoritative handoff)

_Last updated: 2026-10-31 (lead integration cycle 2: #28 #32 #36 #29 #30 #31 #38 merged)_

## Where things stand
All roadmap phases 0–10 are implemented. An independent audit on 2026-10-30
([docs/AUDIT_2026-10-30.md](docs/AUDIT_2026-10-30.md)) found and fixed 18 defects,
including a corporate-action bug that could fabricate a 50% loss, a login-throttling
lockout of all web users, a job-submission deadlock, undeletable workspaces, ML
survivorship bias and look-ahead tests that could not detect a one-bar leak. Its
prioritized backlog supersedes the old next-steps list and is now tracked as GitHub
issues #3-#25 (dependency order in [ROADMAP.md](ROADMAP.md)). Multi-agent ownership,
leases and migration slots: [docs/COORDINATION.md](docs/COORDINATION.md).

> **Security gate (P0, #39): do not expose any deployment publicly.** The trusted TLS edge
> (#3, PR #32) is merged, but exposure stays prohibited until #39 closes. #39 requires:
> * production fails closed on an untrusted peer;
> * the remote probe is an exact-match check;
> * the edge passes `infra/edge/verify.sh --check-remote-probe` from a **second machine**,
>   on a Docker host that preserves client addresses.
>
> This Mac runs Docker Desktop, where that probe correctly fails, so nothing verified here
> can clear the gate. Compose binds every port to 127.0.0.1.

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
* Cycle 1: #1 audit (`656510d`), #2 account-wide quotas (`c4f5460`).
* Cycle 2 (2026-10-31), in merge order:
  * #28 CSV uploads authenticated before the multipart body is read (#4).
  * #32 trusted TLS edge (Caddy overlay; ADR-0008 rewritten). Closes #3 but **not** the
    gate (#39).
  * #36 hand-computed research-correctness suite (#35).
  * #29 undefined statistics instead of rounding noise (#13).
  * #30 deflated Sharpe on a matched window and rf (#14).
  * #31 permanent instrument identity, part A of #9: library and store only.
  * #38 tracebacks no longer log frame locals. Before this, a 500 during login logged the
    plaintext password.
* Every merge had an independent read-only lead review: no blockers. Should-fix items are
  tracked in #8 (Agent 2), #39 (gate) and #41 (Agent 3).

## Last verified (2026-10-31, main `e7d3d82`)
* The combined trial merge of #28, #32, #36, #29, #30 and #31 on `40565cd` gave a tree
  **identical** to main after the merges (`93ca7be`):
  * ruff, format and mypy clean;
  * **pytest 314 passed** (Postgres, slot 85);
  * web lint, typecheck and build clean; `edge.test.mts` 4/4;
  * **Playwright 12/12** with `--workers=1`, at load 6.5–16, OOMKilled=false.
* #38 on top: ruff and mypy clean; the logging, security and edge suites (31) passed. The
  new test fails on the previous main.
* CI is green on main at `93ca7be`. It doesn't run Playwright (#17).
* Agent 2's earlier E2E failures were resource starvation, not regressions:
  * 4 parallel Playwright workers, each registration an Argon2id hash (64 MiB × 4 lanes),
    load 9–12;
  * registrations took 25 s, against 0.56 s alone;
  * each failing spec passed when rerun alone;
  * OOMKilled was false in every run.

  Full-stack runs are now serialized with `make e2e-serial` (docs/COORDINATION.md).

## Next steps (issues carry owners and acceptance criteria)
1. **Agent 2:**
   * #37 invitations (M1): needs a merge from main after #32, then lead review.
   * Then #39 gate code items, #6 (jobs.py lease), #7 (after #27, tasks.py lease), and the
     #8 nits.
2. **Agent 3:**
   * #27 corporate-action gates (tasks.py lease): needs a merge from main after #31.
   * Then merge main into the stacked drafts #34 (#12) and #33 (#11) for review.
   * #9 part B (delisting returns; no migration). Part C waits for M2, which opens after M1.
   * #41 review follow-ups. #24 market-data benchmarks (quiet window only).
3. **Lead:**
   * integrate #37, #27, #34 and #33;
   * #17 E2E in CI;
   * #16 (likely parallel-worker contention; passes serially);
   * #22 isolation hardening;
   * #25 job-deletion semantics.
4. **Unassigned:** #15, #18, #20, #21, #23, the benchmark half of #24. **Later:** #19
   Terraform, after #39.

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
