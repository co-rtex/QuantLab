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

### Elevated 429s on login
Throttling is per client address and per account (failed attempts only), shared through
Redis. The client address comes from the web tier's `X-Forwarded-For`, accepted only from
`QUANTLAB_TRUSTED_PROXIES` (compose: `["web"]`). If `audit_events.ip` shows one address
for all users, the API is not trusting the proxy: check that setting. Internet-facing
deployments should put an edge that overwrites `X-Forwarded-For` in front of the web tier
(ADR-0008). During a Redis outage limits are per process (ADR-0007).

### Disk usage
`$QUANTLAB_DATA_DIR/datasets` holds immutable dataset versions; `artifacts/` holds
experiment and ML outputs; `uploads/` holds staged CSV imports (swept by the reaper after
24 h). Deleting a dataset version directory breaks experiments that reference it; archive
instead.

## Backup and restore

* PostgreSQL: `docker compose exec postgres pg_dump -U quantlab -Fc quantlab > quantlab.dump`;
  restore with `pg_restore -c -d quantlab quantlab.dump`.
* Data directory: copy `$QUANTLAB_DATA_DIR` (or snapshot the `appdata` volume). Dataset
  versions are content-addressed; after a restore, `DatasetStore.verify()` recomputes each
  hash to detect corruption.
* Redis holds only queue messages and caches. After loss, the reaper re-delivers queued jobs
  from PostgreSQL; caches refill on demand.

## Configuration checklist for production

`QUANTLAB_ENVIRONMENT=production` refuses to start unless `QUANTLAB_JWT_SECRET` is a real
secret (≥ 32 characters, no placeholder words), `QUANTLAB_COOKIE_SECURE=true`, and
`QUANTLAB_METRICS_TOKEN` is set. Terminate TLS at a trusted edge. Run migrations
(`alembic -c services/api/alembic.ini upgrade head`) before starting new API versions.
