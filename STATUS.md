# Status (authoritative handoff)

_Last updated: 2026-10-31 (lead integration cycle 2: 82 PRs merged before this update, per `gh pr list --state merged`; main `db005e4`)_

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
  * #62 jobs keyed by permanent instrument id (#9 part C).
    * M2 migration `9a4c6e2f1b37`; engine 1.3.0; read-only reconciliation API.
    * Merged after two lead conditions: ML excluding the benchmark index, and no
      today's-ticker bias. **#9 complete.**
  * #69 device cookies against targeted lockout, plus ADR-0010 on registration
    enumeration (#55).
  * #70 #8 follow-ups.
  * #71 (lead) integration tests use the worktree's own Postgres when
    `QUANTLAB_PG_PORT` isn't exported.
  * #67 delisting follow-ups (#59): halt convention documented; `after_window` counts
    stale marks only; an opt-in performance-delisting fallback, reported as a warning.
  * #75 `/ready` returns generic statuses and logs the detail. **#8 complete.**
  * #73 Massive adapter correctness before live wiring (#50): short reads retried; no key
    in exception chains. **#50 complete**; live use is still blocked on credentials.
  * #77 statistics follow-ups (#41).
  * #78 (lead) ADR-0011: jobs are deleted only through their workspace, with a guard test
    (#25).
  * #81 (lead) stack-env URLs follow overridden ports and work outside git. **#22
    complete.**
  * #74 the assistant verifier binds each number to what the sentence says it is (#15,
    verifier part). Three review rounds. False rejects in #79; the hosted eval is blocked
    on a key.
  * #76 reconciliation follow-ups (#54).
  * #84 the invitation timing test uses paired samples (#80).
  * #83 API errors surfaced with Retry, accessible (#18).
  * #87 catalog row per (ticker, instrument), migration M3 (#66).
  * #88 reproduction names differing inputs, and the integrity check assesses only
    same-input re-runs (#63).
  * #90 reconciliation tail-step false alarm (#86). #93 comments.
  * #92 the #18 follow-up (catalog keys, dedupe, focus). #89 `/invite` auth errors (#85).
  * #95 vendor-neutral provider plumbing, **inert by default** (#11).
    `hide_input_in_errors` also closed a pre-existing secret echo in settings errors.
  * #94 verifier false rejects (#79), after four review rounds.
  * #97 storage lifecycle on workspace deletion (#21): locked GC, quarantine to `.trash`,
    and the sweep refuses when the DB looks empty (restore safety).
  * #99 the real cause of #16: a `useApi` poll stopped on a transient error before the
    first load. **#16 complete.**
  * #100 tri-state synthetic labels everywhere, failing safe to "unknown" (#23 slice 1).
  * #101 stable catalog paging. #103 per-workspace report storage (#96,
    confidentiality).
  * #104 no stale data across resource changes (#23 slice 2). #105 workspace rename,
    delete and member removal (#23 slice 3a).
  * #106 an authoritative `is_synthetic` on result rows (#102).
  * #107 cancel jobs. #109 labels from the row field. #111 saved strategies. #112 session E2E.
    #114 CSV import. **#23 complete.**
  * #110 benchmark harness: variance, cold mode, load metadata; golden tests moved (#24,
    no figures published).
  * #113 AWS Terraform, validated and **never applied** (#19). Redis AUTH uses an ephemeral
    write-only token. **The Caddyfile is NOT validated.**
  * #116 (lead) the reaper mounts the data volume; it was sweeping an empty `/data`.
  * #119 RDS CA bundle (pinned checksum) for `sslmode=verify-full`.
  * #121 cross-feature point-in-time sweep (#118). It found **#120**: a short halt dropped a
    security from the ML panel for up to 252 sessions. The fix is #125, in review.
  * **#122 (P1 security, #115): no pickle in the job queue.** An allowlisted callable only,
    no callbacks or webhooks, and Redis AUTH.
  * #124 default start date on short datasets.
  * #127 pinned tests for the #122 trusted-job registry and scheduler paths (#115 follow-up).
  * #128 the JWT and metrics secrets are required only by the API process (#117); worker,
    reaper and migrate start without them. Clears the JWT item from ADR-0012's
    "Before any apply" list.
  * #129 a retry is not claimed before its backoff ends (`jobs.not_before`, migration M4
    `4d2b8e6f0c15`). Early or forged deliveries reschedule once and consume no attempt (#123).
* Every merge had an independent read-only lead review: no blockers. Should-fix items are
  tracked in #8, #51, #52 and #55 (Agent 2), #39 (gate), and #41, #50 and #54 (Agent 3).

## Last verified (2026-10-31, main `db005e4`)
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
* #62 (`2047bed`) on main@36525d3, run sequentially: 544 passed; serial E2E 13/13 at load
  about 6; migration round trip head → `7c3e5a1d9b20` → head clean, `alembic check` no drift.
* main@925cc1c + #69 + #70 + #71 (byte-identical to main `77f8650`): **559 passed, exit 0**;
  **serial E2E 13/13** (55 s, load about 7).
* main + #67 + #75: 570 passed, E2E 13/13. main + #73: 583 passed.
* main + #77 + #78 (byte-identical to main `0a1c5f2`): 591 passed, plus 1 failed
  (`test_invite_timing_does_not_depend_on_registration`, a 5.54 vs 5.0 wall-clock margin in
  an untouched file; it passed 5/5 rerun alone). Tracked as a flake (#80). E2E 13/13.
* main + #74: 697 passed, E2E 13/13. main + #76: 737 passed. #84: the timing test passed 3/3
  and `test_invitations` 39/39.
* main + #83: web clean, E2E 17/17 (4 new error specs).
* main + #87 (byte-identical to main `52508ed`): **743 passed**. Fresh-DB migration round
  trip with `NULLS NOT DISTINCT` verified in `pg_constraint`; no drift; **E2E 17/17**.
* #88 + #90: 754 passed, E2E 17/17. #89 + #92: E2E 21/21. #95: 778 passed, E2E 21/21.
* #94: 871 passed. Serial E2E 20/21: `workflow.spec` failed at the validation regime panel
  at **load about 3**, then passed 3/3 alone (an A/B including main). **#16 is a real
  intermittent validation-panel race**, not only contention; it's reassigned to Agent 2.
* #97: 892 passed, E2E 21/21. #99: E2E 23/23 (plus 5 consecutive full runs by Agent 2).
  #100: E2E 26/26.
* #122 + #114: an unauthenticated Redis ping gets NOAUTH; **950 passed**, E2E 40/40.
  #119 + #121: 933 passed plus the intended #120 xfail, E2E 37/37 on the rebuilt image.
  #124: E2E 41/41 (byte-identical to main `2cafc62`).
* main@6c69691 + #127 + #128 (byte-identical to main `ead9020`): **960 passed, 1 xfailed**
  (#120), exit 0; ruff, format, mypy, web lint/typecheck/build clean; **E2E 41/41**.
* main@ead9020 + #129 (byte-identical to main `db005e4`): **964 passed, 1 xfailed**; fresh-DB
  round trip head → `b5e1d7c3a924` → head (`jobs.not_before` present, absent, present),
  `alembic check` no drift; **E2E 41/41**.
* #101 + #103 + #104: 899 passed, E2E 27/27. #105 + #106 (byte-identical to main
  `f7e399f`): **906 passed, E2E 30/30**.

CI is green on every merged head. CI doesn't run Playwright (#17).

E2E failures this cycle were resource starvation, not regressions:
* parallel Playwright workers, registrations' Argon2id hashes queued, load up to 35;
* OOMKilled false in every run;
* each failing spec passed when run serially.

Full-stack runs are now serialized machine-wide (`make e2e-serial`).

## Next steps (issues carry owners and acceptance criteria)
1. **Agent 2:** #130 (database time for leases, `not_before` and claims; until it merges,
   hosts with skewed clocks can claim a retry early or see a live lease as expired).
2. **Agent 3:** #125 (#120 ML own-bar windows). The staleness rule is being revised so it
   targets genuine halts, not sparsely traded names, with a selection table in METHODOLOGY.
3. **Lead:** integrate.
4. **Blocked on the user:**
   * #39 (a host, a second machine, and Caddyfile validation, since the caddy download was
     denied);
   * #17 (Actions minutes);
   * #11 and #12 live (vendor, licence and key);
   * #15 hosted eval (LLM key);
   * #20 (load host);
   * #55 option A;
   * a quiet window for #24 figures;
   * any `terraform apply` (cloud account, cost).

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
