# Release-candidate readiness (2026-10-31)

## Status update (later on 2026-10-31, main `6eaf2d9`)
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
