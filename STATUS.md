# Status (authoritative handoff)

_Last updated: 2026-10-31 (lead integration cycle 2: 26 PRs merged; main `ba13ddb`)_

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
  * #40 idempotent retries return the original run (#6). Advisory lock, then workspace
    lock, then user lock.
  * #42 (lead) `make e2e-serial` with a machine-wide full-stack lock; Playwright defaults
    to one worker; one DB engine per URL.
  * #37 consent-based invitations replace direct add-member (#5), with migration M1
    `7c3e5a1d9b20`. Merged after two lead merge conditions: degraded-mode limiter windows,
    and member lock order.
  * #27 corporate-action placement, ordering and ingestion gates (#10). Engine 1.2.0.
    Merged after four gate false positives were fixed.
  * #44 (lead) integration tests fail instead of silently skipping when Postgres is
    unreachable.
  * #45 security-gate code for #39:
    * production fails closed on an untrusted peer;
    * exact-match remote probe;
    * Caddy `persist_config off`;
    * every page gated.

    **#39 stays open** for host verification.
  * #47 idempotency fingerprint (#43).
  * #48 CSV upload durability (#7).
  * #34 reconciliation report (library; #12 API exposure pending).
  * #33 Massive adapter as a **fixture-only contract**. #50 must be fixed before live
    wiring.
  * #53 (lead) E2E workflow-spec budget (#16).
  * #49 delisting returns from the instrument master (#9 part B). Bit-for-bit unchanged
    without a master (28 hashed runs). Follow-ups in #59.
  * #57 CSV ambiguous-commit race (#51).
  * #58 same-day dividends kept through ingestion (#50 finding 2; this also affected
    existing ingestion).
  * #61 synthetic-route idempotency (#52).
  * #64 hardening batch A (#8):
    * the web proxy streams request bodies with a byte cap;
    * an over-limit chunked body now gets 413, not 400;
    * NUL in upload fields → 422;
    * atomic dev secret file;
    * report CSP `sandbox`;
    * expired invitations purged.
  * #65 uploads recheck `is_active`/`token_version` after the body is read (#8 C).
* Every merge had an independent read-only lead review: no blockers. Should-fix items are
  tracked in #8, #51, #52 and #55 (Agent 2), #39 (gate), and #41, #50 and #54 (Agent 3).

## Last verified (2026-10-31, main `ba13ddb`)
Each merge was verified on a trial merge with current main, with pytest output written to a
file rather than piped:
* #28, #32, #36, #29, #30, #31: tree identical to main afterwards; 314 passed;
  Playwright 12/12.
* #38: 31 targeted passing; the new test fails on the previous main.
* main + #27 + #40 + #42: 369 passed; Playwright 12/12 via `make e2e-serial`.
* #37 head (merged with main): 366 passed, 0 skipped; `test_migrations` 2/2; Playwright
  13/13 via `make e2e-serial` at load 13–20.
* main (with #37) + #27 + #44: **418 passed, exit 0**, ruff, format and mypy clean;
  **Playwright 13/13**.
* main + #45 + #47: 431 passed. The E2E `workflow.spec` timeouts were bisected and then checked
  with an interleaved A/B (main 26 s and 48 s; +#45 40 s and 44 s): no regression. The
  cause is #16's 90 s budget (fix: #53).
* main + #47 + #48 + #34 + #33 (`06ad9ca`, byte-identical to main `72b934b`): **486
  passed, exit 0**; **Playwright 13/13**.
* #49 on main: 510 passed. main + #57 + #58 (byte-identical to main `337e9fe`): **514
  passed, exit 0**. E2E not rerun (library-only and route-only changes).
* One full serial run on main@5e07deb was 11/13 and is **contaminated**: the lead ran a
  pytest suite at the same time. Recorded on #16; it doesn't count either way.
* main + #61: 519 passed. main + #64 + #65 (`25b7aec`, byte-identical to main `ba13ddb`):
  **531 passed, exit 0**; web lint, typecheck and build clean; serial E2E 12/13. The
  failing `a11y.spec` timed out at load 22, then passed alone under the lock in 49 s.
* Lead rule after two contaminated runs: no full pytest on any slot while holding the
  full-stack lock.

CI is green on every merged head. CI doesn't run Playwright (#17).

E2E failures this cycle were resource starvation, not regressions:
* parallel Playwright workers, registrations' Argon2id hashes queued, load up to 35;
* OOMKilled false in every run;
* each failing spec passed when run serially.

Full-stack runs are now serialized machine-wide (`make e2e-serial`).

## Next steps (issues carry owners and acceptance criteria)
1. **Agent 2:**
   * #55: device cookies, plus documenting the registration 409. ADR approved.
   * The #8 follow-up (dev-secret race, `os.link` fallback, `req.signal`, 410s).
   * #8 PR B (`/ready`, after #62).
2. **Agent 3:**
   * **#62 (#9 part C): changes requested.**
     * ML trains on the benchmark index, because `exclude` isn't resolved to ids.
     * Today's-ticker bias for tickers a security is renamed into.
   * #59 (delisting follow-ups);
   * #66 (part C follow-ups);
   * #63;
   * #9 part C (M2 open; `tasks.py`, `models.py` and `schemas.py` leased; also #12's
     report API);
   * #50 (P0, Massive adapter fixes; the same-day dividend collapse also affects existing
     ingestion);
   * #54, #41, #24.
3. **Lead:**
   * #16: needs clean serial full-suite runs on main with nothing else heavy running;
   * #17 E2E in CI (private-repo Actions minutes: confirm with the user);
   * the rest of #22;
   * #25.
4. **Unassigned:** #15, #18, #20, #21, #23, the benchmark half of #24.
5. **User-only:** #39 host verification (Linux Docker Engine plus a second machine);
   credentials for #11 (vendor), #15 (LLM key), #19 (cloud), #20 (load host).

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
