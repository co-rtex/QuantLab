# Release-candidate readiness (updated 2026-11-01)

## Release candidate 2 (2026-11-01, main `87449b2`)

**Release SHA:** `87449b2927b988c611757d2835bcc2de9a2049ac`, proposed tag `v0.1.0-rc.2`.
Versions unchanged from rc.1: packages and web `0.1.0`, engine **1.6.0**, ML pipeline v3,
Alembic head `18869f6a1dae` (no migration since rc.1). Scope unchanged: private,
self-hosted use; **public deployment stays prohibited** (#39, #168).

**Verdict: READY as a private release candidate.** It fixes all three of rc.1's caveats,
each after review, a lead trial and green CI, and each re-checked live by the rc.1 assessor
on this commit.

**Changes since rc.1** (six PRs, one combined trial):
* #243 (#237): the leakage check restricts an explicit universe to the members listed at
  each cut, so validation no longer fails for a late listing. An independent review ran all
  9 strategies with mid-sample listings: every multi-decision strategy verified with 0
  mismatches, and a planted late-listing leak and the existing planted leak still fail.
  buy_and_hold stays "unassessed" (one decision).
* #241 (#238): the assistant's verifier reads the sign as written, so "-0.00" for -0.0041 is
  supported, while wrong signs are still flagged.
* #242 and #246 (#239): `is_synthetic` on portfolio create; 429 `rate_limited` with
  Retry-After; one-line CSV failures; "50 MiB"; the CSV limits and a container-safe restore
  in README and RUNBOOK.
* #244 (#233): in a linked worktree a bare `docker compose` resolves to the worktree's
  project, never `quantlab`. A **separate clone** still defaults to `quantlab` unless it
  sets `QUANTLAB_SLOT` or `-p` (documented in README).
* #245 (#235): the edge overlay pins `QUANTLAB_ENVIRONMENT=production`.

**Evidence**
* **The lead's combined trial** of the six PRs on `cbc3a2d`. Its merge tree is identical to
  `87449b2`'s (`183f402b`), so it is a gate on this exact tree:
  * ruff, format and mypy clean;
  * pytest 1633 passed, 152 skipped (the same oracle slow-mode and opt-in stack skips as
    rc.1);
  * tests/unit alone 1003 passed;
  * the slow-mode oracle, noise and buy-scale tests: 219 passed, 1 skipped;
  * web 21/21; Playwright 49/49.
* **The rc.1 assessor's live re-check on `87449b2`** (slot 88):
  * #237: its repro and a 100-symbol daily experiment complete validation with the leakage
    check verified, including a cut before the late listing;
  * #238: an answer with "Sharpe ratio: -0.00" is `grounded: true`, and the sign checks
    still flag "-0.01" for +0.0141, "0.01" for -0.0141 and "-0.00" for +0.0041;
  * #239: every item observed as specified (create `is_synthetic: true`; 429 with
    `retry-after`; one-line CSV failures; a 413 saying "50 MiB"; the docs);
  * #244: a bare compose in a separate clone prints `quantlab`, as documented.
* rc.1's full fresh-clone assessment (below) still applies to everything rc.2 did not
  change.

**Known limitations:** as rc.1's list below, minus #237, #238 and #239. Open post-RC:
#218, #224, #225 (PR #248), #227, #231 and #247.

## Private release candidate (2026-11-01, main `7fc89ee`)

**Release SHA:** `7fc89eed5639f8feca4bd1b49c5e5a1c4303603e`. Versions: packages and web
`0.1.0`, engine **1.6.0**, ML pipeline v3, Alembic head `18869f6a1dae`. Scope: **private,
self-hosted use** on one host, every port bound to 127.0.0.1, trusted users. **Public
deployment stays prohibited** (#39 security gate; #168 account lifecycle).

**Verdict: READY as a private release candidate, with caveats.** Proposed tag `v0.1.0-rc.1`
on `7fc89ee`.

* The lead's integrated gate and an independent assessment from a fresh clone both pass on
  this exact commit. The assessment found **no release blocker for private use**: every core
  workflow ran live, crash recovery worked, and synthetic data was labelled on every surface
  it checked, including the assistant and both report formats.
* **Caveats** (each fails closed, never with wrong figures; fixes planned for rc.2):
  * #237 (P1): validation fails, with an explicit error, for an experiment whose explicit
    universe includes a symbol first listed after a sampled leakage-check cut. Workaround:
    no explicit universe (the UI default).
  * #238 (P2): the assistant marks its own answer ungrounded when a value rounds to "-0.00".
  * #239 (P3): minor API messages and documentation gaps.
  * This Mac's Docker VM (3918 MiB) is below the documented 5120 MiB minimum. Every run here
    passed, with no OOM kill.

### Evidence
**Lead integrated gate** (lead-trial worktree, slot 86, the fullstack lock held; run on
`7fc89ee` itself):
* ruff, format (247 files) and mypy (77 files) clean;
* pytest **1599 passed, 152 skipped**, 0 failed. The skips are the 151 slow-mode cases of
  the engine oracle (they run under `QUANTLAB_ORACLE_SLOW=1`, below) and the opt-in
  `tests/stack` restart test (run against a live stack, below);
* tests/unit alone 975 passed; `QUANTLAB_ORACLE_SLOW=1` oracle, noise-order and buy-scale
  tests 219 passed, 1 skipped;
* migrations on a fresh database: base → head `18869f6a1dae` → base (1 table left, the
  version table) → head; `alembic check`: no new upgrade operations;
* web: lint, typecheck, unit tests **21/21** and build clean;
* serial Playwright **49/49**.

**Upgrade and reproduction across the engine bump** (Agent 3, slot 31, under the lock):
an experiment created on `73238c0` (engine 1.5.0) and reproduced on `7fc89ee`. No
migrations were needed (`18869f6a1dae` before and after). `/reproducibility`:
`same_inputs: false` with `engine_version` 1.5.0 → 1.6.0 as the only difference,
`fingerprint_match: true`. `/integrity`: verified 6, warning 3, failed 0, unassessed 6, with
reproducibility "unassessed" and the documented reason. `tests/stack`: 1 passed (reproduce
and validate across an API and worker restart).

**Independent assessment** (fresh clone, slot 88): **READY WITH CAVEATS**, no blockers.
* Clean install: `make setup`, lint, typecheck, web lint/typecheck/build exit 0; web tests
  21/21; `npm audit --omit=dev` 0 vulnerabilities (16 moderate, dev-only, in the full audit).
* Migrations: fresh base → head → base → head and `alembic check` clean; on a database with
  data, one revision down and back up kept every row (the downgrade drops
  `jobs.attempt_started_at` by design).
* Startup: `make app-up` in 21.8 s; health and readiness ok; every published port on
  127.0.0.1; API docs 404 under `test` and `production`; a per-request CSP nonce.
* Workflows (API): register, workspace, synthetic and CSV ingest, a momentum backtest
  (engine 1.6.0), reproduction (`same_inputs` and `fingerprint_match` true), validation,
  integrity (16 checks), HTML and Markdown exports (both labelled synthetic), the assistant
  (`synthetic: true`), ML (best out-of-sample AUC 0.530), a portfolio and optimize.
* Limits: 200 symbols daily for 2005–2023 → 422 `backtest_too_large` (1228.2 MB over 1228),
  199 → 202; 2000 synthetic assets → 422 `dataset_too_large`; a 53,477,463-byte CSV → 413;
  two concurrent optimizes on one API process → 200 and 503 `server_busy` with
  `retry-after: 2`; the optimize rate limit 10 then 429.
* Recovery: a SIGKILL of a worker's main process mid-job; Docker restarted it in about 3 s,
  and the job completed on attempt 2. API restarts kept state.
* Tests: pytest 1599 passed, 152 skipped; `make e2e-serial` 49 passed.

### Merged since the 2026-10-31 status update
Each behaviour change had an independent review, a lead trial on current main and green CI.
* **Engine:** 1.5.0 (#209: #187 lot check at fill, #195 exact pro-rata factor with
  `commission_min`, #196 large-account float tolerance) and 1.6.0 (#226, #215: no order for
  a float-noise change in a holding). The differential oracle catches all 32 mutations.
* **Admission:** #192 (#181) closes two fail-open cases; 7-day weeks cap daily runs at 137
  symbols.
* **Jobs:** #213 (#212) RQ timeouts take the timed-out path; #216 (#214) a horse the worker
  killed is never classed as OOM; #208 (#200) explicit API Redis timeouts.
* **Security and limits:** #204 (#201) API docs off outside development; #205 (#202) the
  console logger; #211 (#207) the limiter breaker and floor (ADR-0007, #221); #219 (#217)
  cash-only portfolios read only the trading dates (one valuation was +1085 MiB);
  #228 (#223, #220) a non-blocking per-process bound on heavy requests (503 `server_busy`
  with Retry-After), an optimize rate limit and an API memory limit.
* **Labelling and verification:** #232 (#230) synthetic data labelled on every surface,
  including assistant answers; #193 (#182) byte-identical outputs across processes, hash
  seeds, threads and restarts; #222 (#210) a warning for several dividends on one ex-date;
  #236 deterministic limiter tests; #229 and #234 RC docs audits.

### Requirements
* macOS or Linux with Docker; **Docker VM memory ≥ 5120 MiB** for the default stack (2 job
  workers at 1536 MiB, the API at 1880 MiB with 2 uvicorn workers, about 110 MiB for
  Postgres, Redis, web and the reaper; docs/RUNBOOK.md, "Resources"). The 4-worker API
  override needs 2560M and a 5742 MiB VM. `scripts/stack-env.sh` warns below the minimum.
  This development Mac's VM is 3918 MiB, so every run here was below the documented
  minimum (they passed).
* Python via `uv`, Node for the web app (`make setup`); Postgres 17 and Redis from compose.
* Secrets set as in `.env.example` before any non-development use; the production guard
  refuses placeholders and short secrets.

### Known limitations (not release blockers for private use)
* **No public exposure** until #39 closes; no password change or reset, and a plain logout
  leaves a bearer token valid until it expires (#168).
* **Synthetic and CSV data only.** The licensed vendor adapter and the reconciliation
  against a second source are blocked on a licence and key (#11, #12). The assistant uses
  the deterministic provider; a hosted model needs a key and approval (#15).
* **Capacity:** heavy requests run one per API process (two per container by default);
  extra concurrent ones get 503 `server_busy` and the web retries once. One optimize at 100
  symbols costs 285–590 MiB (#225), and optimize still runs in the API (#224). Backtest
  admission caps memory-heavy runs (about 199 symbols daily for 2005–2023).
* **Benchmarks:** the published figures measured `da8767a` (engine 1.4.0); large/xl and
  external load tests are not done (#24, #20).
* Open at rc.1: #237, #238, #239 (all fixed in rc.2, above), and #218, #225, #227, #231,
  #233, #235 (owners and priorities on each).

## Status update (later on 2026-10-31, main `6eaf2d9`)
_Historical. Superseded by the section above: every issue listed below as open or in
progress is now closed (#187, #195, #196, #181/#192, #200, #201, #202, #165, #166, #175),
and #191's 4.5 GiB minimum became 5120 MiB in #228._
**Both code-defect release blockers are fixed and merged.** Each had an independent
review, a trial on current main (full pytest and serial Playwright) and green CI.

**Fixed:**
* **#162 (private-use blocker), in #170.** A worker gets a unique name per start
  (`<host>-<pid>-<hex>`) and a restart policy. Worktree dev slots default to no restart,
  so they don't come back with Docker Desktop. The main checkout and the edge overlay
  keep `unless-stopped`. Crash probe: workers rejoined in 4.7–6.3 s, and the job
  completed on attempt 2.
* **#163, in #174.**
  * Engine memory down 23–26%, with results bit-for-bit identical in an independent
    24-scenario A/B (no engine version bump).
  * OOM kills are classified as `ResourceExhausted`, not retried.
  * Admission is bounded to the measured range. The cost: for 2005–2023 the effective
    caps are about 199 symbols daily and 400 otherwise, where the documented limit was
    1500.
* **#164, in #172:** the adjuster look-ahead test. The suite now catches all 8 injected
  financial defects.
* **#167, in #171:** the job timeout is per attempt (migration M5).
* **#160, in #176:** the benchmark contended check.

**Job-recovery confirmation (independent, fresh clone, slot 88, main `6fcdd07`, after
#165): ready, with caveats.** Every probe was run live:
* **Crash (#162):** an OOM kill of the worker's pid 1. Docker restarted it, and it rejoined
  in 2.3 s with 0 "active worker" errors; the job completed on attempt 2. After a manual
  `docker compose kill`, it rejoined 3.1 s after the RUNBOOK command.
* **Admission (#163):** 200 symbols daily gets 422 `backtest_too_large` (an estimate of
  1228.1 MB against the 1228 limit); 199 is admitted.
* **OOM in a horse (#163),** with the probe's worker limit lowered to 700 MB:
  `ResourceExhausted`, not retried, and the worker isn't restarted. Two later jobs on the
  same worker completed, so fork safety holds.
* **Per-attempt timeout (#167):** a 223 s outage with a 150 s timeout. The job was
  requeued, not timed out, and attempt 2 completed.
* **Cancel:** about 0.1 s, idempotent, nothing stored.
* **Redis outage:** a submission during it was durable and redelivered, and the in-flight
  job was recovered on attempt 2. Caveat #185: busy workers crash-loop during the outage,
  and the job loses an attempt.
* **Metrics (#165):** outcome counters match the probes, `jobs_running` reads 0 after
  crashes, and Prometheus discovers both workers. The reaper's retries aren't exported
  (#179).
* **Regression:** E2E 45/45; job tests 23 + 30 passed.
* Disclosure: the assessor pulled an `alpine:3` image the brief didn't allow, to read the
  VM's swap. The lead removed it.

**Integrated gate (lead, slot 86, main `e7cf4fd` = `6fcdd07` + #183's CI step):**
* ruff, format and mypy clean;
* pytest **1366 passed, 0 skipped**;
* migrations: fresh DB → head `18869f6a1dae` → base → head, `alembic check` with no drift;
* web: lint, typecheck, unit tests 6/6 and build clean;
* serial Playwright **45/45** at load about 4.5.

**Merged since (each with independent review, trial on current main, CI green):**
* **#190 (#180, #184):** an independent reference backtester with a differential test and
  a mutation harness (25/25 mutations caught), and engine **1.4.1**: dust cash is no cash,
  and buys cut to nothing are rejected `insufficient_cash`.
* **#191 (#175):** a documented minimum of 4.5 GiB of Docker VM memory, measured, with a
  warning-only check. A VM-level OOM was not reproduced.
* **#189 (#179, #194):** reaper metrics exported, each terminal outcome counted once.
* **#197 (#185):** busy workers survive a Redis outage without losing the job's attempt,
  including a SIGTERM during the outage.
* **#198 (#186):** no double delivery from the reaper.
* **#199 (#166):** a per-request nonce Content-Security-Policy.
* **#183:** web unit tests in CI.

**Found by the reference backtester and its reviews (open, engine 1.5.0 in progress, one
release):**
* #187: phantom whole-share top-ups shrink real buys. Fix: the lot check at fill time.
* #195: pro-rata ignores `commission_min` on sells, and the scaling factor double-counts
  it. Fix: an exact factor, defined in METHODOLOGY.
* #196: large-account buys rejected whole for float-level shortfalls (shares vs dollars
  tolerance).

**Other open defects:**
* #181/#192: two fail-open admission cases (ticker reuse; 7-day-week rebalances). Fixed in
  #192, merging after 1.5.0.
* #201 (P1, security): Swagger UI served same-origin without a CSP. A fix is in progress
  (docs off outside development).
* #202 (P1, local stacks): `log.exception` raises under the console logger. A fix is in
  progress.
* #200: explicit API Redis timeouts. Today they are 5 s by redis-py default.

**Still blocking public deployment** (unchanged; none of these are code defects):
* #39: external infrastructure;
* #168: a product decision on account lifecycle;
* AWS credentials and cost, if AWS is the target.

**Open, non-blocking:**
* #165: worker metrics, in progress.
* #166: CSP.
* #175: container memory limits against the Docker VM size.
* Large, xl and load benchmarks need a quiet window under the new rule.

The assessment below is unchanged, as written at `da8767a`.

**Verdict.** QuantLab at `0939811` is **not yet a release candidate**.
* **Private, self-hosted use:** ready once one code defect is fixed. Workers can't rejoin for
  about seven minutes after an unclean exit, and nothing restarts them (#162).
* **Public deployment:** stays **prohibited**. Besides #162 it needs the #39 edge
  verification on external infrastructure, a fix for jobs that exhaust worker memory
  (#163), and a product decision on account lifecycle (#168).

## How this was assessed
An independent assessor (a fresh agent with no part in the integration work) worked from
a fresh clone of `origin/main` at `da8767a` and its own stack (slot 88, every port on
127.0.0.1), under the full-stack lock.
* Every claim below comes from a command it ran or a file it read.
* Nothing was pushed, and no paid service, hosted model or cloud account was used.
* Main moved to `0939811` during the run. The only change was #161 (benchmark results and
  docs, plus one benchmark script's API URL), so the findings apply to `0939811`.

## Results by area
| Area | Verdict | Evidence | Gaps |
|---|---|---|---|
| Installation | ready | From a fresh clone: `make setup`, `make lint`, `make typecheck`, and web `lint`/`typecheck`/`build` all exit 0 | The README dev-process path (`api-dev`, `worker`, `web-dev`) was not run |
| Migrations | ready | One head (`4d2b8e6f0c15`). On a fresh database: base → head, then eight stepwise down/up steps, all exit 0; `alembic check` reports no drift | Downgrades not tried on a database with data |
| Authentication | ready, with caveats | Argon2id (m=65536, t=3, p=4). HS256 JWT, 1 h TTL; tampered and `alg=none` tokens get 401. Logout-everywhere revokes; the address (10/min) and account (30/min) limits hold, and a known device still signs in. The production guard refuses every placeholder or short secret tried | No password change or reset (#168). A plain logout leaves a bearer token valid until it expires |
| Authorization | ready | Route matrix: 243 passed. Live, 17 cross-workspace and body-swap probes by an outsider all got 404; unauthenticated requests get 401 | — |
| Dataset ingestion | ready | Synthetic and CSV ingestion work, with `is_synthetic` labels. NaN volume and negative or inconsistent prices fail the quality gates; an empty licence gets 422; a 51 MiB upload gets 413 | — |
| Financial correctness | ready, with caveats | Unit and golden: 721 passed; integration: 557 passed. A hand-checked backtest matches to 1e-6 (pro-rata buys, costs, dividend, split, zero-volume rejection). Mutation testing caught 7 of 8 injected defects (next-session fills, zero-volume fills, delisting date and return, split restatement, dividends, strategy look-ahead) | A one-bar look-ahead injected into `PointInTimeAdjuster.scale_as_of` passes the whole suite (#164). The current code is correct; the test is missing |
| Reproducibility | ready | Idempotent replay works, and a changed body gets 409. A reproduction has the same `input_hash` and an identical fingerprint. A validation records both engine versions, `comparable_engine` and suite version 2. The integrity report's 16 checks were verified | `code_version` is "unknown" in compose unless `QUANTLAB_GIT_SHA` is set |
| Job recovery | **not ready** | The reaper requeues after a lease expires, and redelivery after a Redis outage works. Cancel is idempotent | After a worker is killed, restarted workers exit 1 for about 7 minutes and no service restarts them (#162). A job inside the documented limits was OOM-killed on every attempt and reported as `LeaseExpired` (#163). The timeout counts dead time across retries (#167) |
| E2E | ready, with caveats | `make e2e-serial`: 44 passed, none flaky. Playwright runs in GitHub Actions on relevant PRs (#159; 44/44, and a deliberate failure uploaded its artifacts) | E2E runs on PRs only, not on main, and is not yet a required check (#17) |
| Security | private: ready. Public: **blocked** | Runtime `pip-audit` and `npm audit` are clean (#138; blocking in CI, #139). Redis requires AUTH. Forged RQ jobs were refused live. The edge `verify.sh` passes locally, and production returns 503 `edge_required` to an untrusted peer. The Caddyfiles pass local adapt, validate and PROXY v1/v2 tests (#158) | #39 needs external infrastructure. No CSP on web pages (#166). Base images use floating tags |
| Operations | ready, with caveats | `pg_dump` restored into a scratch database with identical counts. Benchmarks published for small and medium only (#161) | Worker metrics are never exported, so the Grafana job panels stay empty (#165). No alert rules. Resource limits only on workers. Large, xl, load and scaling benchmarks are unpublished (#160). External load untested (#20) |

## Release blockers
Each blocker has exactly one kind.

### Blocks a private or self-hosted release candidate
| # | Kind | Blocker | Smallest next step |
|---|---|---|---|
| 1 | CODE DEFECT | **#162** Workers can't rejoin after an unclean exit: the name is `hostname:pid`, and pid is 1 in a container. No compose service has a restart policy | A unique worker name per start, plus `restart: unless-stopped`; rerun the kill-worker probe (Agent 2) |

### Blocks public deployment as well
| # | Kind | Blocker | Smallest next step |
|---|---|---|---|
| 2 | EXTERNAL INFRASTRUCTURE | **#39** The edge must be verified on an address-preserving host, from a second machine, and for AWS behind a real NLB. The code side is done (fail-closed, exact-match probe, local Caddyfile checks) | The user provides a Linux Docker host and a second machine |
| 3 | CODE DEFECT | **#163** A backtest within the documented limits exhausts worker memory, so one account can occupy every worker | Measure memory, then bound admission or reduce memory (Agent 3) |
| 4 | PRODUCT DECISION | **#168** Account lifecycle: no password change or reset, no admin tooling. Email verification (#55 option A) is deferred | The user decides what a public release needs |
| 5 | MISSING CREDENTIALS | AWS account and cost approval, only if the target is AWS (#19, never applied) | The user's decision; `terraform apply` stays blocked |

### Not blockers: features that wait on the user
* #11 and #12: a licensed market-data vendor and reconciliation source (LICENSING, MISSING
  CREDENTIALS). Paid data stays blocked.
* #15: a hosted-model evaluation (MISSING CREDENTIALS). Hosted LLM use stays blocked.
* #20: an external load test (EXTERNAL INFRASTRUCTURE: a separate host).
* #17: making Playwright a required check (a branch-protection change; the user's
  go-ahead).

### Non-blocking improvements (fix before or soon after a release)
* #164: a test for the adjuster look-ahead.
* #165: export worker metrics.
* #166: a Content-Security-Policy.
* #167: the per-attempt job timeout.
* #160: benchmark contention that counts the run's own load.
* Set `QUANTLAB_GIT_SHA` in `make app-up`.
* Pin base image digests.
* Resource limits and alert rules for the remaining services.
* Wire `apps/web/src/lib/edge.test.mts` into CI.
* The full-stack lock is per repository, not per machine. A separate clone gets its own
  lock (`scripts/fullstack-lock.sh`).

## Documentation the assessment found out of date
Fixed in the same change as this file:
* STATUS: the main sha, the next steps, and "CI doesn't run Playwright".
* RUNBOOK and ROADMAP still said the AWS Caddyfile was unvalidated.
* ROADMAP: the Terraform phase, #24's state and #17's state.
* ADR-0012 still said RQ uses pickle; #122 replaced it with JSON and a trusted-job allowlist.
* STATUS called the full-stack lock machine-wide.

Left to the issues that change the code:
* the RUNBOOK's dead-worker recovery text (#162);
* the metrics promises in ARCHITECTURE and RUNBOOK (#165).

Also left open:
* RUNBOOK restore uses a host `pg_restore`, which this machine doesn't have; `docker
  compose exec -T postgres pg_restore` works.
* `DatasetStore.verify()` checks one version, not all.
* TEST_MATRIX's "as-of adjustment" coverage (#164).

## Not verified
* The README dev-process path.
* Live retry backoff (covered by `test_job_backoff` and `test_jobs_reliability`).
* Downgrades with data.
* The remote edge probe from a second machine, and a real NLB.
* AWS, hosted models and paid data. All prohibited.

## Gates that stay closed (user decisions, 2026-10-31)
* No public deployment.
* No `terraform apply`.
* No paid market data.
* No hosted LLM use.
* Registration option A deferred.
* No weekly scheduled dependency audit.
