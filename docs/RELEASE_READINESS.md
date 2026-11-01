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

**Remaining before a private release candidate:**
* An independent confirmation of the job-recovery area (crash, OOM, cancel, Redis outage)
  on merged main. It will run after #165 (worker metrics), which changes the worker.

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
