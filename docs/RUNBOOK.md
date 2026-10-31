# Operations runbook

## Components and health

| Check | Command | Healthy |
|---|---|---|
| API liveness | `curl localhost:8000/api/v1/health` | `{"status":"ok"}` |
| API readiness (DB + Redis) | `curl -i localhost:8000/api/v1/ready` | 200, both checks `ok` (503 when degraded) |
| Workers | `docker compose --profile app ps worker` / `rq info` as below | workers listening on `quantlab` |
| Reaper | logs contain `reaper_cycle` lines when it recovers anything | process running |
| Metrics | `curl -H "Authorization: Bearer $QUANTLAB_METRICS_TOKEN" localhost:8000/metrics` | Prometheus text |

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
heartbeats, retry backoffs, the reaper's cutoffs, claims and `created_at`. When reading
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

### Jobs stuck in `running`
A worker died mid-job. Its lease (default 60 s, `QUANTLAB_JOB_LEASE_SECONDS`) expires in
database time. The reaper then does one of four things:
* marks the job `cancelled` if a cancel was requested;
* marks it `timed_out` once it has run past `timeout_seconds`;
* fails it once `max_attempts` is reached;
* otherwise requeues it to run at once, with no backoff.

A late heartbeat or commit from the dead worker is fenced out by the attempt counter, so
no duplicate result can be written.

### A job keeps failing
`GET /api/v1/jobs/{id}` → `error.type`, `error.message`, `error.trace` and `retryable`.
Retryable errors are retried with the backoff above until `max_attempts`. Permanent
errors (invalid input, missing dataset) are not retried. Fix the input and submit a new
experiment; results of failed jobs are never stored.

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
passing through the edge. During a Redis outage limits are per process (ADR-0007).

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

* PostgreSQL: `docker compose exec postgres pg_dump -U quantlab -Fc quantlab > quantlab.dump`;
  restore with `pg_restore -c -d quantlab quantlab.dump`. **Stop the worker and reaper
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
  delivery), the login rate-limit counters, and caches. After a loss:
  * the reaper re-delivers queued jobs from PostgreSQL, a retry once its backoff has passed;
  * rate limits start again from zero;
  * caches refill on demand.

  A replacement Redis needs the same password (see "Jobs stuck in `queued`").

## Configuration checklist for production

`QUANTLAB_ENVIRONMENT=production` refuses to start every process (API, worker, reaper,
migrations) unless `QUANTLAB_COOKIE_SECURE=true` and the Redis URL carries a real password
(16+ characters, from `QUANTLAB_REDIS_PASSWORD` in compose; #115). The **API** also
refuses unless `QUANTLAB_JWT_SECRET` is a real secret (≥ 32 characters, no placeholder
words) and `QUANTLAB_METRICS_TOKEN` has 16+ characters. Workers and the reaper never sign
tokens and do not need either (#117); without the key a process fails closed instead of
using a development key. Terminate TLS at the trusted edge (below). Run migrations
(`alembic -c services/api/alembic.ini upgrade head`) before starting new API versions.
Keep every clock synchronised (NTP), the database server's above all: every job deadline
follows it (#130). Hosts' clocks still date tokens and the storage sweep's file times.

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

**Precondition: the edge is verified.** Never publish the web tier, the API, Postgres or
Redis directly; every port in the base `docker-compose.yml` is bound to 127.0.0.1 for that
reason. A client that reaches the web tier without the edge would choose the address the
API throttles on: it could exhaust a victim's per-address bucket and, by rotating
addresses, spend a victim account's failed-login budget (#3).

**Host requirement.** The edge must see each client's real source address. Rootful
Linux Docker with iptables NAT (the default) preserves it for remote clients. Docker
Desktop, rootless Docker with its default port driver, and docker-proxy (connections from
the same host) replace it with a gateway address. Every user would then share one bucket,
and ten failed logins would lock everyone out. Step 3 checks this.

1. Start the stack with the edge overlay, which publishes only the edge and runs the API
   in production mode:
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
4. Only then bind the edge publicly: `QUANTLAB_EDGE_BIND=0.0.0.0 QUANTLAB_EDGE_PORT=443`.

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
  public name before announcing it. That Caddyfile has not been checked with
  `caddy adapt` yet; do that before building the edge image.
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
