# Operations runbook

## Components and health

| Check | Command | Healthy |
|---|---|---|
| API liveness | `curl localhost:8000/api/v1/health` | `{"status":"ok"}` |
| API readiness (DB + Redis) | `curl -i localhost:8000/api/v1/ready` | 200, both checks `ok` (503 when degraded) |
| Workers | `docker compose --profile app ps worker` / `rq info -u $QUANTLAB_REDIS_URL` | workers listening on `quantlab` |
| Reaper | logs contain `reaper_cycle` lines when it recovers anything | process running |
| Metrics | `curl -H "Authorization: Bearer $QUANTLAB_METRICS_TOKEN" localhost:8000/metrics` | Prometheus text |

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

### Jobs stuck in `queued`
1. `GET /api/v1/ready`: if Redis is down, submissions are still recorded durably.
2. Restore Redis; the reaper re-delivers queued jobs older than 2 minutes
   (`reaper_cycle redelivered=N`). Duplicate deliveries are harmless (atomic claim).
3. If no reaper is running, run `quantlab-cli reaper --interval 15`, or execute one job
   synchronously for diagnosis: `quantlab-cli run-job <job-id>`.

### Jobs stuck in `running`
A worker died mid-job. Its lease (default 60 s) expires; the reaper requeues the job (or
fails it once `max_attempts` is reached, or marks it `timed_out` past `timeout_seconds`).
A late heartbeat or commit from the dead worker is fenced out by the attempt counter, so
no duplicate result can be written.

### A job keeps failing
`GET /api/v1/jobs/{id}` → `error.type`, `error.message`, `error.trace` and `retryable`.
Permanent errors (invalid input, missing dataset) are not retried. Fix the input and
submit a new experiment; results of failed jobs are never stored.

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
experiment and ML outputs; `uploads/` holds staged CSV imports (swept by the reaper after
24 h). Deleting a dataset version directory breaks experiments that reference it; archive
instead.

## Backup and restore

* PostgreSQL: `docker compose exec postgres pg_dump -U quantlab -Fc quantlab > quantlab.dump`;
  restore with `pg_restore -c -d quantlab quantlab.dump`.
* Data directory: copy `$QUANTLAB_DATA_DIR` (or snapshot the `appdata` volume).
  Without `QUANTLAB_JWT_SECRET` (development), the signing key is generated into
  `$QUANTLAB_DATA_DIR/.jwt-dev-secret` and travels with these backups; treat them as
  secret, or delete the file after a restore to rotate the key (signs everyone out). Dataset
  versions are content-addressed; after a restore, `DatasetStore.verify()` recomputes each
  hash to detect corruption.
* Redis holds only queue messages and caches. After loss, the reaper re-delivers queued jobs
  from PostgreSQL; caches refill on demand.

## Configuration checklist for production

`QUANTLAB_ENVIRONMENT=production` refuses to start unless `QUANTLAB_JWT_SECRET` is a real
secret (≥ 32 characters, no placeholder words), `QUANTLAB_COOKIE_SECURE=true`, and
`QUANTLAB_METRICS_TOKEN` is set. Terminate TLS at the trusted edge (below). Run migrations
(`alembic -c services/api/alembic.ini upgrade head`) before starting new API versions.

In production, login and registration accept only requests that arrive through the web
tier (a trusted proxy); a direct call to the API, such as
`scripts/case_study.py --api http://localhost:8000` on the host, gets 503 `edge_required`
(ADR-0008, #39). Run such tools against the edge, or against a development stack.

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
