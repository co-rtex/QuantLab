# Operations runbook

## Components and health

| Check | Command | Healthy |
|---|---|---|
| API liveness | `curl localhost:8000/api/v1/health` | `{"status":"ok"}` |
| API readiness (DB + Redis) | `curl -i localhost:8000/api/v1/ready` | 200, both checks `ok` (503 when degraded) |
| Workers | `docker compose --profile app ps worker` / `rq info` as below | workers listening on `quantlab` |
| Reaper | logs contain `reaper_cycle` lines when it recovers anything | process running |
| Metrics (API) | `curl -H "Authorization: Bearer $QUANTLAB_METRICS_TOKEN" localhost:8000/metrics` | Prometheus text |
| Metrics (each worker; job, dataset, ingest) | for each replica N (1, 2, …): `docker compose exec --index N worker python -c "import os,urllib.request as u; r=u.Request('http://localhost:9101/metrics', headers={'Authorization': 'Bearer ' + os.environ.get('QUANTLAB_METRICS_TOKEN', '')}); print(u.urlopen(r).read().decode())"` (port 9101 is never published; without `--index`, compose picks one replica) | Prometheus text with `quantlab_job_outcomes_total`; Prometheus target `quantlab-worker` up for every replica |
| Metrics (reaper; lease-loss retries, the outcomes it decides) | `docker compose exec reaper python -c "import os,urllib.request as u; r=u.Request('http://localhost:9102/metrics', headers={'Authorization': 'Bearer ' + os.environ.get('QUANTLAB_METRICS_TOKEN', '')}); print(u.urlopen(r).read().decode())"` (port 9102 is never published) | Prometheus text with `quantlab_job_retries_total`; Prometheus target `quantlab-reaper` up |

### Inspecting the job queue

The queue holds JSON entries that only `TrustedJob` will run (#115). The `rq` command line
tools default to pickle and to RQ's base `Job`, so pointed at a Redis someone has tampered
with they unpickle job meta and results *on your machine*. Always:

```
export RQ_REDIS_URL="$QUANTLAB_REDIS_URL"       # from the environment, never `-u <url>`:
                                                # a command line, and its password, shows in `ps`
export RQ_JOB_CLASS=quantlab_api.jobs.TrustedJob
rq info -S rq.serializers.JSONSerializer
rq requeue --all -S rq.serializers.JSONSerializer   # only with both variables set
```

Never point plain `rq` tools, or rq-dashboard (it reads jobs with the default serializer),
at this Redis.

## Resources

**Docker VM memory: at least 5 GiB (5,120 MiB) for the default stack (#175, #220).** The
default is 2 workers at 1536 MiB, 2 API processes in a 1880 MiB container, and the 1228 MB
backtest admission cap.
* **The contract:** the sum of the memory limits, plus the measured peaks of the services
  without one, must fit the VM. Then no container can push the VM itself into an OOM:
  3,072 (workers) + 1,880 (API) + about 110 (Postgres, Redis, web, the reaper, measured
  below) = 5,062 MiB, rounded up to 5 GiB.
  * The previous minimum, 4.5 GiB, predates the API's limit. The API then had none, and
    16 concurrent optimizations drove the VM into a global OOM (#223; see "503
    `server_busy` and the API's memory limit").
  * With 4 API processes (`QUANTLAB_API_WORKERS=4`), the API needs
    `QUANTLAB_API_MEMORY=2560M` (measured peak 2,121 MiB plus 20%), and the VM about
    5.6 GiB (3,072 + 2,560 + 110 = 5,742 MiB).
* **The measurement** (2026-11-01, main e7cf4fd, a 3.83 GiB / 3,919 MiB VM): 3 rounds of
  2 concurrent backtests at the admission cap (equal weight, 199 symbols, daily, 2005–2023).
  * All 6 completed on their first attempt, with no OOM kill and no container restart.
  * The VM's `MemAvailable` fell to 234 MB at worst: 94% of the VM in use (3,685 MiB).
  * Each worker peaked at 1,083–1,160 MiB anonymous; the API at 335 MiB, the reaper 47,
    Postgres 28, web 19, Redis 15.
* **The previous minimum** was derived from that peak: 3,685 MiB plus 20% is 4,422 MiB,
  rounded up to 4.5 GiB (4,608 MiB).
* **Below the minimum, as on this project's development machine (3.83 GiB):** the limits
  don't fit, so a VM-level OOM is possible again. It shows in the VM's kernel log as
  `Out of memory: Killed process` with `global_oom`, rather than `Memory cgroup out of
  memory` with `CONSTRAINT_MEMCG`. It happened there on 2026-11-01, with the API still
  unlimited (#223).
* **Not measured on top:** the observability profile (Prometheus, Grafana, Jaeger), a
  second stack, and Docker builds. Each adds to the same VM.
* **The warning:** `scripts/stack-env.sh`, and so every `make` target, warns on stderr when
  the VM is smaller than the minimum. It changes nothing; `QUANTLAB_SKIP_VM_CHECK=1`
  silences it.
* **To fit a smaller VM:** run 1 worker (`QUANTLAB_WORKERS=1`), or lower the worker limit
  together with `QUANTLAB_MAX_BACKTEST_MEMORY_MB` (see "Backtests that don't fit the
  worker's memory").
* **Resource Saver:** Docker Desktop stops an idle VM (5 minutes by default here), and its
  kernel log goes with it. To keep evidence of an OOM kill, read
  `docker run --rm --cap-add SYSLOG --entrypoint dmesg redis:7-alpine` before every
  container is stopped.

### 503 `server_busy` and the API's memory limit (#223, #220)
* **What `server_busy` means:** a heavy request reached an API process whose heavy-work
  slots were all taken. It was refused at once, before doing any of its work, never queued:
  `503`, `{"error": {"code": "server_busy", …}}`, with `Retry-After: 2`.
  * **The web app** says "The server is busy with other analyses. Retrying in N s…" and
    sends the request again once after that wait, which is safe because nothing was done.
  * **Heavy routes:** `POST /dataset-versions/{id}/optimize`, and the portfolio ledger,
    which values a ledger over full price histories: `POST /workspaces/{id}/portfolios`,
    `POST /portfolios/{id}/transactions` (its slot is taken before the row lock) and
    `GET /portfolios/{id}/valuation`.
  * **Counted in** `quantlab_heavy_requests_refused_total{route}`.
* **The settings:**

  | Setting | Default | What it sets |
  |---|---|---|
  | `QUANTLAB_HEAVY_REQUESTS_PER_PROCESS` | 1 | heavy requests at once per API process |
  | `QUANTLAB_API_WORKERS` | 2 | API processes |
  | `QUANTLAB_API_MEMORY` | 1880M | the API container's memory limit |
  | `QUANTLAB_RATE_LIMIT_OPTIMIZE_PER_MINUTE` | 10 | optimizations per user and minute; beyond it `429 rate_limited` |

* **The AWS deployment** (`infra/terraform`) has the same shape: the ECS API task has 2048 MiB
  (`ecs.tf`), `api_workers` defaults to 2 (`variables.tf`), and the heavy bound is the app's
  default of 1. That fits the 1,542 MiB measured below.
* **When to raise `QUANTLAB_HEAVY_REQUESTS_PER_PROCESS` or `QUANTLAB_API_WORKERS`:** only when
  `server_busy` refusals are frequent, and only together with `QUANTLAB_API_MEMORY`,
  measured again as below.
  * An optimization at its maximum (100 symbols, `lookback` 1260, 4.6M bars) took
    285–590 MiB of anonymous memory each (#225).
  * Example: 4 processes at 2 slots each in 2048M were OOM-killed inside the API (below).
  * The VM must hold the sum of all limits plus the other services ("Resources").
* **An API OOM kill**, measured with 4 processes at 2 slots in 2048M, 16 concurrent
  optimizations:
  * **What happens:** the kernel kills one or more API processes inside the container, not
    any other container. The VM's kernel log shows `CONSTRAINT_MEMCG` (`Memory cgroup out of
    memory`), never `global_oom`, and `docker inspect` sets `OOMKilled: true`.
  * **Recovery:** uvicorn logs `Child process [N] died`, then starts a new process
    (`Started server process [M]`). The container keeps running (`RestartCount` 0) and
    serves the next requests.
  * **What the user sees:** the requests that the killed process was serving fail. The
    client sees a dropped connection, and the web app shows "The server could not be
    reached" with Retry.
  * **If the container itself exits** (its first process killed), its restart policy brings
    it back: `unless-stopped`, or not on a worktree slot (`no`).
* **The measurement behind the defaults** (2026-11-01, slot 21, 4.6M-bar version):
  * Each optimization: 100 symbols, `lookback` 1260, 4 methods, sent all at once.
  * Every run had an API limit set, so an OOM could only be the API's. The figures are
    anonymous memory: page cache fills `memory.current` up to the limit and is reclaimed.

  | Configuration | Request load | Outcome | Anonymous memory |
  |---|---|---|---|
  | **Before** (no bound), 2048M | 16 optimizations | 12 dropped connections, `oom_kill +3`, `CONSTRAINT_MEMCG +3`, `global_oom +0` | peak 1933 MiB |
  | 2 processes, 1 slot each, 2048M, first run (the limit's source: 1,566 × 1.2 = 1,879.2, so 1880M) | 4 optimizations | 2 × 200, 2 × 503 | peak 1566 MiB, idle 394 |
  | Defaults, `QUANTLAB_RATE_LIMIT_OPTIMIZE_PER_MINUTE=1000` (many users) | 16 optimizations | 2 × 200, 14 × 503 | peak 1529–1542 MiB, idle 392; within 1880M |
  | Defaults, `QUANTLAB_RATE_LIMIT_OPTIMIZE_PER_MINUTE=1000` (many users) | 8 optimizations + 8 valuations of a 50-symbol portfolio | 2 × 200, 14 × 503 | peak 657 MiB |
  | Defaults, as shipped (one user) | 16 optimizations | 1 × 200, 6 × 429, 9 × 503 | peak 1008 MiB |
  | 4 processes, 1 slot each, 2280M | 16 optimizations | 4 × 200, 12 × 503 | peak 2121 MiB; hence 2560M for that override |

  * **No OOM** of any kind in any run with the bound at 1 per process.

## Observability

* **Logs**: JSON (structlog) with `request_id` and, when tracing is on, `trace_id`.
  A job's logs carry the `request_id` of the HTTP request that submitted it.
* **Traces**: set `QUANTLAB_OTEL_ENDPOINT=http://<collector>:4318/v1/traces`. The API
  instruments every request; the trace context is stored on the job, so `job.execute` and
  `job.handler` spans from the worker appear in the same trace as the submitting request.
* **Local stack**: `echo -n "$TOKEN" > infra/prometheus/metrics_token` then
  `QUANTLAB_METRICS_TOKEN=$TOKEN QUANTLAB_OTEL_ENDPOINT=http://jaeger:4318/v1/traces docker compose --profile app --profile observability up -d`.
  Jaeger UI http://localhost:16686, Prometheus http://localhost:9090, Grafana
  http://localhost:3001 (dashboard "QuantLab operations": API p95 by route, 5xx share, job
  outcomes, queue wait, run time, retries, DB p95, cache hit rate, dataset freshness).

## Incidents

Job timing runs on PostgreSQL's clock, not any host's (#130). That covers leases,
heartbeats, retry backoffs, the reaper's cutoffs, claims (`started_at`,
`attempt_started_at`) and `created_at`. When reading
these columns, compare them with `SELECT now()` in the database, not with `date` on a host.
A worker, reaper or API host with a skewed clock no longer moves any job deadline.

### Jobs stuck in `queued`
0. **Check it isn't waiting out a retry.** A job whose `message` reads
   `retrying in Ns after <error>` is in its backoff: 2 s, then 4 s, and so on, never more
   than 60 s. It isn't stuck. Its `jobs.not_before` column holds when it may run (#123), and
   no worker claims it before then, however early a delivery arrives.
1. `GET /api/v1/ready`: if Redis is down, submissions are still recorded durably. Redis
   requires AUTH (#115). A replacement Redis must use the same `QUANTLAB_REDIS_PASSWORD`
   (`--requirepass`), or readiness stays 503 with `ready_check_failed component=redis` and
   an authentication error, and nothing can be enqueued.
2. Restore Redis. The reaper then re-delivers each queued job (`reaper_cycle
   redelivered=N`) once all of these hold, in database time:
   * it was created more than 2 minutes ago;
   * its last delivery or heartbeat was more than 2 minutes ago;
   * its backoff, if any, has passed.

   Duplicate deliveries are harmless (atomic claim).
3. If no reaper is running, run `quantlab-cli reaper --interval 15`. To execute one job
   synchronously for diagnosis, use `quantlab-cli run-job <job-id>`. It goes through the
   same claim as a worker, so it prints `skipped` and runs nothing for a job that isn't
   claimable: one still in its backoff, one cancelled, or one another worker holds a live
   lease on.

### Redis is down (#185)
* **Running jobs carry on.** A job's state, lease and keepalive are in PostgreSQL, so a job
  that was running when Redis went away completes on the same attempt.
* **Workers stay up.** Each one logs `worker_redis_unavailable` (with `during=`: the job's
  heartbeat, the worker's state, maintenance and so on), and waits for Redis before taking
  the next job.
  * **Before #185,** a busy worker quit at its horse monitor's first heartbeat in the
    outage, within 30 s (`job_monitoring_interval`), so an outage of more than a few
    seconds was enough. Its container restarted in a loop, the running job died with it and
    lost an attempt, and it waited out its lease.
  * **Measured on 2026-11-01** (slot 21, two workers, restart policy `unless-stopped`): Redis
    was stopped for about 63 s during a running ML job. The job stayed on attempt 1 and
    completed on attempt 1, 45 s after Redis returned. Both workers' restart counts stayed
    0, and they logged 8 `worker_redis_unavailable` lines and no "unhandled exception,
    quitting".
* **New submissions** are still recorded, and readiness reports 503 (see "Jobs stuck in
  `queued`" for what is redelivered once Redis is back).
* **Rate limits** (logins, registration, invitations, the assistant, optimize): each API process's
  limiter breaker opens at its first connection error (#207): breaker open = counting
  in-process, still limiting. See the hung-Redis entry below.
* **RQ's own records** in Redis, of jobs that finished during the outage, stay incomplete
  (`rq info` may show them as started). RQ's registry cleanup marks them failed later.
  That changes nothing: a job's outcome is the one in PostgreSQL.
* **A SIGTERM during the outage** (`docker compose stop`, a deploy) waits for the running
  job, as it does without one (#197 review), but only within Docker's stop timeout: 10 s
  by default (`docker compose stop -t <s>`). After that Docker kills the container, and the
  job is recovered like any lost worker's (see "Jobs stuck in `running`").
* **A worker started or restarted while Redis is down** can't register with it. It exits,
  and its restart policy starts it again, in a loop until Redis is back. With
  `QUANTLAB_RESTART_POLICY=no`, a worktree slot's default, it stays stopped.
* **Retries scheduled for later** wait for RQ's scheduler. Its process dies at its first
  Redis error, and the worker restarts it only in maintenance, up to 600 s later. Until
  then such a retry is delivered by the reaper instead, about 2 minutes after its backoff
  ends (see "Jobs stuck in `queued`").
* **A refused password** (a rotated `QUANTLAB_REDIS_PASSWORD`) is not treated as an
  outage, but only a busy worker stops on it.
  * **A busy worker** stops at the first authentication or authorization error during a
    job, after waiting for the running job to finish.
  * **An idle worker** doesn't stop. RQ's own dequeue retries every connection error,
    these included, so it logs `invalid username-password pair` (or `NOPERM`) in a loop
    and takes nothing.
  * **To tell it from an outage:** `/api/v1/ready` reports 503 with `redis: error`, and
    the API logs `ready_check_failed component=redis` with `AuthenticationError` in
    `error`, where an outage logs `ConnectionError` or `TimeoutError`.
  * **The API's rate limiter** treats it like an outage: `AuthenticationError` is a
    connection error, so its breaker opens (breaker open = counting in-process, still
    limiting), and `rate_limiter_breaker_open` in the API's log shows the error.
* **A Redis that accepts connections but doesn't answer** (a hung server, a network that
  drops packets) is different from one that is down.
  * A connection attempt gives up after 5 s (`QUANTLAB_REDIS_CONNECT_TIMEOUT_SECONDS`).
  * **The API:** each call gives up after 5 s (`QUANTLAB_REDIS_SOCKET_TIMEOUT_SECONDS`),
    once, without retries (#200). Then:
    * `/api/v1/ready` reports 503 with `redis: error`;
    * the rate limits (logins, registration, invitations, the assistant, optimize) are counted in
      each API process instead. Breaker open = counting in-process, still limiting (#207):
      a connection error or a timeout opens each process's limiter breaker, which skips
      Redis for 10 s, then lets one call try it again. Any other Redis error (a bad
      reply) falls back for that call only. `quantlab_rate_limiter_breaker_open` is 1
      meanwhile, with `rate_limiter_breaker_open` and `rate_limiter_breaker_closed` in
      the API's log;
    * bar reads skip the cache (`"cache": "bypass"`);
    * a submitted job stays `queued` until the reaper delivers it, once Redis answers.
  * **How long requests wait.** Without the breaker, a login made three limiter calls (two
    with a wrong password), each waiting a timeout: 15.21 s with Redis paused (#200's
    probe). With it, at the onset every limiter call already in flight waits its own
    timeout (up to as many as the API's thread pool runs at once). Then, per process, one
    probe per 10 s waits up to connect + reply, and other limiter calls don't wait. The
    bars cache, `/ready` and enqueue have no breaker: they still wait on every call.
  * **A worker:** once connected, a call blocks for up to 415 s: RQ raises the connection's
    socket timeout to its dequeue timeout plus 10 s (`worker_ttl` 420 − 15 + 10), so a
    blocking dequeue isn't cut short. That is about 7 minutes per attempt.
  * Meanwhile the horse monitor is blocked, and with it its backstop kill of a horse
    that outlives its job timeout by 60 s. The horse's own job timeout still fires.
  * The job's lease and keepalive, in PostgreSQL, still run.

### Jobs stuck in `running`
A worker died mid-job. Its lease (default 60 s, `QUANTLAB_JOB_LEASE_SECONDS`) expires in
database time. The reaper then does one of four things:
* marks the job `cancelled` if a cancel was requested;
* marks it `timed_out` if its current attempt ran past `timeout_seconds`;
* fails it once `max_attempts` is reached;
* otherwise requeues it to run at once, with no backoff.

**The timeout counts each attempt's run time (#167)**, from `attempt_started_at` (the
attempt's claim) to when its lease ran out (`lease_expires_at`).
* Two kinds of time don't count: waiting for a worker between attempts, and the time since
  the lease expired. So a job requeued after an outage, even one the reaper was down for,
  is retried, not timed out.
* The lease's end counts, rather than the last heartbeat, because with a long
  `QUANTLAB_JOB_LEASE_SECONDS` the keepalive beats only every lease/3. An attempt killed
  for overrunning may have last beaten before the timeout. Measuring to the lease's end
  over-counts by at most one lease.
* `started_at` stays the job's first claim. Rows from before `attempt_started_at` existed
  are timed from `started_at`.
* The worker checks the same limit at each progress heartbeat, and RQ kills an attempt at
  timeout + 60 s.

A late heartbeat or commit from the dead worker is fenced out by the attempt counter, so
no duplicate result can be written.

### A worker died or was restarted
* **A crash or OOM kill restarts by itself.** The core services run with
  `restart: unless-stopped`, so Docker restarts a worker that crashes or is OOM-killed. In
  the #162 probe, both workers were back 4.7 s after a SIGKILL.
  * This holds for the main checkout and for the edge overlay (`infra/edge/compose.yml`),
    which pins `unless-stopped`.
  * A linked worktree's stack runs with `restart: no` instead, because
    `scripts/stack-env.sh` sets `QUANTLAB_RESTART_POLICY=no` for any slot but 0. That way a
    Docker restart doesn't bring back every agent's dev stack at once.
  * `QUANTLAB_RESTART_POLICY` overrides either default. A crash-recovery probe on a
    worktree slot must set `QUANTLAB_RESTART_POLICY=unless-stopped` explicitly.
* **A stop you ran stays stopped.** Docker treats `docker compose stop` and
  `docker compose kill` as manual stops. Start the workers again with
  `docker compose --profile app up -d --no-deps worker`, which leaves the API and
  databases as they are.
* **A restarted worker rejoins at once.** Each start takes a new RQ name,
  `<container hostname>-<pid>-<random>`.
  * The dead process's name stays in `rq info` as a stale worker until its registration
    expires, 8 minutes later. It holds no work.
  * Its job is recovered as in the previous section, once its lease expires. In the probe,
    the job completed on attempt 2 about 90–100 s after the kill.
* **`QUANTLAB_WORKER_NAME` fixes the name instead.** Use it only for a single worker that
  is always stopped cleanly. After an unclean exit, RQ refuses that name until the stale
  registration expires. Until then the worker exits 1, and with the restart policy Docker
  keeps restarting it. The name must not contain `:` and is at most 128 characters.

### A job keeps failing
`GET /api/v1/jobs/{id}` → `error.type`, `error.message`, `error.trace` and `retryable`.
Retryable errors are retried with the backoff above until `max_attempts`. Permanent
errors (invalid input, missing dataset) are not retried. Fix the input and submit a new
experiment; results of failed jobs are never stored.

### Backtests that don't fit the worker's memory (#163)
* **At submission:** `422 backtest_too_large`, for either of two reasons, given in
  `details.reason`.
  * **`estimated_memory`:** the estimated peak memory exceeds
    `QUANTLAB_MAX_BACKTEST_MEMORY_MB`.
    * The estimate grows with symbols × sessions loaded and symbols × rebalances in the
      window.
    * Sessions, and rebalances in the window, are each the larger of the exchange
      calendar's count and the version's distinct bar dates (the engine rebalances on its
      bar dates, #181). On a 7-day-week dataset the daily cap for 2005–2023 is therefore
      137 symbols, not 199.
    * Symbols are the job's instruments: a universe's distinct tickers, or every
      instrument of the version (a reused ticker once per holder).
    * The estimate in a refusal is rounded up to 0.1 MB, so it is never shown equal to
      the limit.
    * It ignores the strategy, so a strategy that holds few names is overestimated.
    * The default limit is 1228 MB, which is 80% of the compose worker's 1536 MB limit.
    * `details` give the estimate, the limit, and the symbols, sessions and rebalances.
  * **`outside_measured_range`:** the backtest has more than 400 symbols, or symbols ×
    sessions or symbols × rebalances above 400 × 4,785. Those products are what was
    measured, so larger backtests are refused rather than estimated.
    * A long history of few symbols stays inside: per-session costs that don't scale with
      symbols are about 1 KB a session.
    * A new measurement (below) can widen the range.
* **What reduces a backtest's memory:** fewer symbols, a less frequent rebalance or an
  earlier end date. The job loads the data from its first bar up to the end date, whatever
  the start. So a later start doesn't reduce what is loaded, and helps only by cutting
  rebalances.
* **Effective caps for 2005–2023** (4,785 sessions) under the defaults, of the 1,500-symbol
  universe limit:

  | Rebalance | Most symbols | What decides it |
  |---|---|---|
  | daily | 199 | the estimate |
  | weekly | 400 | the measured range (the estimate alone would allow 407) |
  | monthly | 400 | the measured range (the estimate alone would allow 514) |
  | quarterly | 400 | the measured range (the estimate alone would allow 542) |

  An earlier end date allows more, as does a dataset with less history.
* **Older experiments:** reproducing one that is larger than these now returns the same
  422, although it ran before #163.
* **If a job still runs out of memory:** the kernel kills its work horse, and the job fails at
  once as `ResourceExhausted` ("out of memory: the job was stopped after using N MB of the
  worker's M MB limit"), not retryable.
  * **The evidence:** a SIGKILL, plus either a rise in the container's `memory.events`
    `oom_kill` count, or the horse's peak resident set (`ru_maxrss`) at 95% or more of
    `memory.max`.
  * **It needs cgroup v2** (`/sys/fs/cgroup/memory.events`, as in the compose workers). On a
    host with cgroup v1, possibly AWS Fargate, those files don't exist, and the
    classification silently never fires: such jobs fail as `LeaseExpired` after their
    retries, as before. Check before relying on it there.
  * **Before #163,** such a job was retried twice into the same wall and then failed as
    `LeaseExpired`.
  * A worker lost for any other reason still recovers through the reaper as above.
* **Raise the limit only together with the worker's memory.**
  * Measure first with `benchmarks/worker_memory.py` on the new worker: it samples each job's
    anonymous memory in the real container.
  * Then set `QUANTLAB_MAX_BACKTEST_MEMORY_MB` to keep the same 20% headroom.
  * Widening the measured range (`MEASURED_MAX_SYMBOLS`, `MEASURED_MAX_SESSIONS`) also
    needs measured jobs out there, with sessions varied as well as symbols. Then refit.
  * The estimate's coefficients (`quantlab_api/limits.py`) come from the measured envelope
    in `benchmarks/results/*-worker-memory-after-*.json`.

### ML runs fail with "submitted for pipeline version N"
Deploy the API and the workers together, at the same release. The API stamps its
`ML_PIPELINE_VERSION` into each ML run's config and input hash. A worker running a
different version refuses the run permanently (#143), instead of storing a result whose
hash describes another panel. ML runs in flight across a pipeline-version change therefore
fail with "this ML run was submitted for pipeline version N, but this worker runs version
M" and must be resubmitted. A resubmission is a new run, and only a reused
`Idempotency-Key` replays the old one. One-time caveat: during the first deploy of this
check, workers older than it do not know the stamped `pipeline_version` field, so they
fail such runs with a config validation error instead. Resubmit those the same way.

Backtests work the same way (#152): an experiment records the `ENGINE_VERSION` its input
hash was computed with, and a worker on another engine fails it permanently with "this
backtest was submitted for engine version N, but this worker runs version M". Resubmit
it, or use Reproduce. A replayed `Idempotency-Key` returns the failed job. Workers older
than this check do not refuse, so during the deploy that introduces it a run can still
be computed on a different engine than it is stored under.

### 429 `too_many_jobs` on submissions
Queued plus running jobs are capped per workspace (`QUANTLAB_MAX_ACTIVE_JOBS_PER_WORKSPACE`,
default 50) and per account across all its workspaces. The account cap is
min(`QUANTLAB_MAX_ACTIVE_JOBS_PER_USER`, `QUANTLAB_MAX_ACTIVE_JOBS_PER_WORKSPACE`), so
lowering the workspace cap also lowers every user's account-wide cap; otherwise extra
workspaces would multiply one account's allowance. A replay of an idempotent request is
never refused because of the job it created.

### Elevated 429s on login
If one user reports 429 with the right password while others can sign in, someone is
spending that account's failure budget (`user.login_failed` events for its domain from
several addresses). Browsers that signed in before keep working (device cookies,
ADR-0008); ask the user to sign in from one of those. Logging out everywhere also revokes
those device cookies, so do not use it as a remedy during such an attack.

Throttling is per client address and per account (failed attempts only), shared through
Redis. Behind the TLS edge the client address is the edge's TCP peer, which the edge
writes into `X-Forwarded-For`; the web tier forwards it only with a valid edge secret, and
the API accepts it only from `QUANTLAB_TRUSTED_PROXIES` (compose: `["web"]`). Without an
edge (local development) no address is forwarded and only the per-account limit applies
(ADR-0008). If `audit_events.ip` shows one address for all users behind the edge, either
the Docker host replaces client source addresses (the address is a Docker gateway; see
"Exposing a deployment", host requirement), or the API is not trusting the web tier (the
address is the web container's; check that setting and that `web` resolves from the API). A 403 `edge_required` from the web tier means a request reached it without
passing through the edge. Limits are per API process (ADR-0007) during a Redis outage,
and for 10 s after any connection error to Redis (the limiter's breaker, #207: breaker
open = counting in-process, still limiting). Each key starts from the floor: the last
count Redis returned to that process in this window, or 0 for a key it hasn't seen.

### Disk usage
`$QUANTLAB_DATA_DIR/datasets` holds immutable dataset versions; `artifacts/` holds
experiment and ML outputs; `uploads/` holds staged CSV imports. Deleting a dataset version
directory by hand breaks experiments that reference it; archive instead.

Deleting a workspace removes its files after the database commit (#21,
`quantlab_api.storage`):
* Its experiment, ML and staging artifacts are removed, along with the uploads staged
  for its jobs.
* Dataset version directories are content-addressed and shared when two workspaces hold
  the same slug and content. One is removed only when no version row references it any
  more. That is decided under the directory's advisory lock (`dataset-dir:<slug>/<hash>`),
  which `register_version` also holds from before its write through its commit.
* Reports live per version row under `datasets/.reports/<row id>/` (#96). A deleted
  workspace's report folders are removed with it, and the sweep collects folders whose
  row is gone, with the same refusal rules. Files at the old shared location
  (`<slug>/_reports/<hash>/`) are no longer served; they go when their version
  directory does.
* If the process dies between the commit and the deletion, only unreferenced files
  remain. The reaper's `sweep_orphans` (at most once per
  `QUANTLAB_STORAGE_SWEEP_INTERVAL_SECONDS`, default an hour) collects unreferenced
  version directories, leftover `.tmp-*` and `.old-*` directories, and artifacts whose
  row is gone, once older than the grace period. That period is twice the longest
  timeout of any queued or running job, and at least 24 h; every version write runs
  inside a job, so none can outlast it.
* Nothing is deleted at once. Removed files move to `$QUANTLAB_DATA_DIR/.trash/<time>/`
  (names URL-encoded from their path under the data directory) and are purged after
  `QUANTLAB_STORAGE_TRASH_RETENTION_SECONDS` (default 7 days), hourly by the reaper even
  while the sweep is off. **Disk space is reclaimed only after that retention period.**
  To undo a mistaken removal, move them back.
* `.trash` must be on the same filesystem as `datasets/` and `artifacts/` (it is, under
  one `QUANTLAB_DATA_DIR` volume). Quarantine is a rename; across mounts it fails with
  `EXDEV`, is logged, and nothing is freed.
* The hourly markers (`.last-storage-sweep`, `.last-trash-purge`) are file mtimes. A
  marker dated in the future after the clock steps back counts as due, so it never freezes.
  Staging directories are temporary and are quarantined without the refusal check.
* The sweep refuses, logs `storage_sweep_refused` at error level and removes nothing,
  when the database has no rows but the disk has files (a restore in progress, or the
  wrong `QUANTLAB_DATABASE_URL`), or when one cycle would remove more than
  `QUANTLAB_STORAGE_SWEEP_MAX_FRACTION` (default 25%) of what is on disk. Advisory locks
  are per database, so never point two stacks with different databases at one
  `QUANTLAB_DATA_DIR`. Set `QUANTLAB_STORAGE_SWEEP=false` to switch the sweep off. After
  checking a refusal, run `quantlab_api.storage.sweep_orphans(force=True)` by hand.
* The upload sweep never deletes the files of a queued or running job, however old.
  Other staged uploads go after 24 h.

## Backup and restore

* PostgreSQL, through the container (no host PostgreSQL tools needed):
  * **Dump:** `docker compose exec -T postgres pg_dump -U quantlab -Fc quantlab > quantlab.dump`.
  * **Restore:** `docker compose exec -T postgres pg_restore -U quantlab -c -d quantlab < quantlab.dump`.
  * **`-T`** keeps a TTY off the binary stream.
  * **These `docker compose` commands act on the current shell's project.** In a linked
    worktree, run `eval "$(scripts/stack-env.sh)"` first, or add
    `-p quantlab-<worktree>`, or they reach the main checkout's database (#233).

  **Stop the worker and reaper
  first** (`docker compose stop worker reaper`), or set `QUANTLAB_STORAGE_SWEEP=false`, and start
  them again after the restore. While tables are dropped and reloaded the database has
  no rows for the files on disk; the sweep refuses to act on that, but don't rely on it.
* Data directory: copy `$QUANTLAB_DATA_DIR` (or snapshot the `appdata` volume).
  Without `QUANTLAB_JWT_SECRET` (development), the signing key is generated into
  `$QUANTLAB_DATA_DIR/.jwt-dev-secret` and travels with these backups; treat them as
  secret, or delete the file after a restore to rotate the key (signs everyone out). Dataset
  versions are content-addressed; after a restore, `DatasetStore.verify()` recomputes each
  hash to detect corruption.
* Redis holds no durable state: only queue messages (including retries scheduled for later
  delivery), the rate-limit counters (logins, registration, invitations, the assistant,
  optimize), and caches. After a loss:
  * the reaper re-delivers queued jobs from PostgreSQL, a retry once its backoff has passed;
  * rate limits start again from zero;
  * caches refill on demand.

  A replacement Redis needs the same password (see "Jobs stuck in `queued`").

## Configuration checklist for production

`QUANTLAB_ENVIRONMENT=production` refuses to start every process (API, worker, reaper,
migrations) unless `QUANTLAB_COOKIE_SECURE=true` and the Redis URL carries a real password
(16+ characters, from `QUANTLAB_REDIS_PASSWORD` in compose; #115). The **API** also
refuses unless `QUANTLAB_JWT_SECRET` is a real secret (≥ 32 characters, no placeholder
words) and `QUANTLAB_METRICS_TOKEN` has 16+ characters. A **worker that serves job
metrics** (`PROMETHEUS_MULTIPROC_DIR` set, as in compose) also refuses without
`QUANTLAB_METRICS_TOKEN`, under the same rule (#165). A worker without that directory, such
as the Terraform ECS task, serves nothing and needs no token. The **reaper** follows the same
rule when `QUANTLAB_REAPER_METRICS_PORT` is set, as in compose (#179). Workers and the reaper never
sign tokens and are not given the JWT key (#117); without the key a process fails closed
instead of using a development key. Terminate TLS at the trusted edge (below). Run migrations
(`alembic -c services/api/alembic.ini upgrade head`) before starting new API versions.
Keep every clock synchronised (NTP), the database server's above all: every job deadline
follows it (#130). Hosts' clocks still date tokens and the storage sweep's file times.

Two settings exposure depends on, which no startup guard checks:
* **`QUANTLAB_TRUSTED_PROXIES`:** the peers allowed to state the client address. It defaults
  to `[]`, and compose sets `["web"]`. Outside compose, set it to the web tier's address, or
  every production login fails closed with 503 `edge_required`.
* **`QUANTLAB_EDGE_SECRET`:** the secret the edge adds and the web tier checks. Caddy
  requires 32+ characters (`infra/edge/compose.yml`), but the web tier accepts any non-empty
  value. Generate it as in "Exposing a deployment", step 1.

In production, login and registration accept only requests that arrive through the web
tier (a trusted proxy); a direct call to the API, such as
`scripts/case_study.py --api http://localhost:8000` on the host, gets 503 `edge_required`
(ADR-0008, #39). Run such tools against the edge, or against a development stack.

### Licensed market data (inert by default)

The vendor and licence are chosen by the user; until then this path is inert. The
`dataset.vendor` job refuses with "licensed market data is not configured (unset: ...)"
unless all four are set:

| Variable | Meaning |
|---|---|
| `QUANTLAB_MARKET_DATA_PROVIDER` | A registered vendor (today only `massive`). An unknown name fails at startup |
| `QUANTLAB_MARKET_DATA_API_KEY` | The key, from a secret store or environment only. Blank counts as unset; production refuses a provider without one |
| `QUANTLAB_MARKET_DATA_LICENSE` | The licence the user holds, recorded in every version's provenance |
| `QUANTLAB_MARKET_DATA_PLAN` | The purchased plan, recorded with it |
| `QUANTLAB_MARKET_DATA_REQUESTS_PER_MINUTE` | Optional: the plan's documented rate limit (not verified here) |

The key is sent only as `Authorization: Bearer`. It is never in a URL, job payload,
heartbeat, log line, provenance or error. There is no API route or UI entry yet; live
behaviour is unverified (no test makes a network call).

## Exposing a deployment

> **Public exposure is prohibited until #39 is closed.** The steps below are what #39
> requires on the host: the edge overlay on an address-preserving Linux host,
> `infra/edge/verify.sh`, and the remote probe from a second machine. #39 also requires the
> lead's independent rerun of both, and, for AWS, a real NLB sending PROXY v2. Passing them
> yourself does not lift the prohibition; closing #39 does.

**Precondition: the edge is verified.** Never publish the web tier, the API, Postgres,
Redis, or the observability profile's services directly: Prometheus (`:9090`, no
authentication), Grafana (`:3001`, with anonymous Viewer access) and Jaeger (`:16686`, and
its unauthenticated OTLP ingest on `:4318`). Every port in the base `docker-compose.yml` is
bound to 127.0.0.1 for that reason, and the edge overlay publishes none of them. A client that reaches the web tier without the edge would choose the address the
API throttles on: it could exhaust a victim's per-address bucket and, by rotating
addresses, spend a victim account's failed-login budget (#3).

**Host requirement.** The edge must see each client's real source address. Rootful
Linux Docker with iptables NAT (the default) preserves it for remote clients. Docker
Desktop, rootless Docker with its default port driver, and docker-proxy (connections from
the same host) replace it with a gateway address. Every user would then share one bucket,
and ten failed logins would lock everyone out. Step 3 checks this.

1. Start the stack with the edge overlay, which publishes only the edge and runs the API
   in production mode. The overlay pins `QUANTLAB_ENVIRONMENT=production` for the API,
   worker and reaper (`infra/edge/compose.yml`, #235), so an exported or `.env` value,
   such as `.env.example`'s `development`, cannot turn off the production guards.
   ```
   export QUANTLAB_EDGE_SECRET=$(openssl rand -hex 32)   # store it like the JWT secret
   export QUANTLAB_JWT_SECRET=$(openssl rand -hex 32) QUANTLAB_METRICS_TOKEN=$(openssl rand -hex 24)
   export QUANTLAB_REDIS_PASSWORD=$(openssl rand -hex 24)   # hex: safe inside the Redis URL
   ```
   A password with other characters (`@`, `/`, `:`, `#`, `%` …) must be percent-encoded
   where it appears in `QUANTLAB_REDIS_URL`, or the URL is read wrongly.
   ```
   docker compose -f docker-compose.yml -f infra/edge/compose.yml --profile app up -d --build
   ```
   For a public domain also set `QUANTLAB_EDGE_SITE=quantlab.example.com` and
   `QUANTLAB_EDGE_TLS=<ACME contact email>`. Caddy then obtains a certificate; the default
   `internal` uses Caddy's local CA, which browsers do not trust.
2. Run `infra/edge/verify.sh` with `COMPOSE_PROJECT_NAME` set, and keep its output. Every
   check must pass:
   * a spoofed `X-Forwarded-For` is ignored;
   * a victim is not throttled by an attacker claiming its address;
   * the web tier refuses requests without the edge secret;
   * the API is unreachable from the edge network;
   * only the edge publishes a port.
3. From a machine outside the host, send the probe in the script's header comment, then
   run `infra/edge/verify.sh --check-remote-probe MARKER REMOTE_IP` on the host, where
   REMOTE_IP is that machine's address as the host should see it. It passes only on an
   exact match; any other recorded address fails. Docker Desktop and rootless Docker can
   record addresses outside Docker's subnets.
4. Only then, and only after #39 is closed, bind the edge publicly:
   `QUANTLAB_EDGE_BIND=0.0.0.0 QUANTLAB_EDGE_PORT=443`.

Behind a CDN or load balancer, the hop that faces the internet must overwrite
`X-Forwarded-For` with its TCP peer. Configure Caddy's `trusted_proxies` with only that
hop's ranges, and use `{client_ip}` instead of `{remote_host}` (see the comments in
`infra/edge/Caddyfile`). Never trust a header just because it is present.

**Residual risks.**
* A failed login counts against both the address (10 per minute) and the account (30 per
  minute), so a client with as few as three real addresses can hold an account at 429. A
  browser that has signed in to that account before is exempt: it presents a device cookie
  and is throttled per device instead (ADR-0008, #55). Only the owner's *new* devices can
  still be held off. IPv6 clients are throttled per /64.
* Registration answers 409 for an address that already has an account, so it reveals
  which addresses are registered: 10 probes per minute per real client address, with no
  global cap, so about 10·N a minute for an attacker with N addresses (an IPv6 /48 holds
  65,536 /64 buckets). Each probe that succeeds creates an audited account (ADR-0010).
  Email verification will close this once a mail adapter exists.

## Cloud deployment (AWS, validated and never applied)
`infra/terraform/` describes an AWS deployment (#19, ADR-0012); its README has the details.
Nothing has been planned or applied against any account. Applying it creates paid
resources and needs the user's account and approval. What differs from compose:
* **Edge:** an NLB terminates TLS (ACM) and sends PROXY protocol v2 to Caddy, which
  accepts that header only from the NLB's subnets and overwrites `X-Forwarded-For` with the
  stated client (`infra/terraform/edge/Caddyfile`). Run `infra/edge/verify.sh` against the
  public name before announcing it. That Caddyfile passes local checks
  (`infra/terraform/edge/check-proxy-protocol.sh`: adapt, validate, PROXY v1/v2), but it
  has not been verified behind a real NLB (#39).
* **Trusted proxies:** the web tier runs in its own subnets, and
  `QUANTLAB_TRUSTED_PROXIES` is exactly those CIDRs. Never schedule other tasks there.
* **Data directory:** EFS, mounted by api, worker **and reaper** (the reaper's sweeps and
  trash purge need it). `.trash` is on the same file system, as quarantine requires.
  Restore AWS Backup recovery points with the reaper scaled to 0.
* **Migrations:** run the `migrate` task once per release before updating services, never
  at API start.
* **Secrets:** Secrets Manager entries are created empty. Set `jwt_secret`,
  `metrics_token`, `edge_secret` and `database_url` before the first deploy. The LLM and
  market-data keys are injected only when enabled; the vendor and licence remain the
  user's choice (#11). Rotate a secret by writing a new version and redeploying the
  services that read it; rotating `jwt_secret` signs everyone out.
* **Redis:** TLS and AUTH, reachable only from api, worker and reaper. It holds login
  rate-limit counters, cached bars and RQ job payloads. The token is generated at apply
  time and stored only in the `redis_url` secret. To rotate it, increase
  `redis_auth_token_version` (`infra/terraform/README.md`).
* **Before any apply:** work through the checklist in `infra/terraform/README.md`: Redis
  AUTH, the Caddyfile and `verify.sh`, and `sslmode=verify-full`.
