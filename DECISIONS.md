# Architecture decision records

Short records of decisions that shape the codebase. Newest last. Each states the
context, the decision and its consequences.

## ADR-0001 Modular monolith plus workers

**Context.** The directive asks for a sophisticated backend without needless microservices.
**Decision.** One Python domain package (`packages/quantlab`, no web/DB imports), one service
package (`services/api`) containing the HTTP API and the RQ worker entrypoints, and a Next.js
frontend. Workers scale independently as separate processes of the same image.
**Consequences.** Simulation logic is testable without services; the API never runs
expensive computation inline.

## ADR-0002 PostgreSQL is the source of truth for jobs; Redis/RQ only delivers

**Context.** RQ provides at-least-once delivery with no transactional link to results.
**Decision.** Job state lives in Postgres. Workers claim with an atomic conditional
`UPDATE ... RETURNING` that increments `attempts` (the fencing token); results and the
terminal status commit in one transaction after re-verifying the token; `experiment_results`
has a unique constraint on `experiment_id`. A reaper requeues expired leases and redelivers
queued jobs whose message may have been lost.
**Consequences.** Duplicate deliveries, crashed workers and Redis outages cannot produce a
second committed result or lose a submitted job (covered by failure-injection tests).

## ADR-0003 Raw prices stored; adjustment derived point-in-time

**Decision.** Bars are stored as traded. Adjusted series are computed from corporate actions
using only events with ex-date <= the as-of date, with the exact total-return factor
`C_t / (s*C_t + D)`. The engine trades raw prices and applies splits/dividends to holdings.
**Consequences.** No look-ahead through adjustment factors; accounting matches what a holder
would experience.

## ADR-0004 Content-addressed immutable dataset versions in Parquet

**Decision.** A dataset version is a directory named by the SHA-256 of a canonical
serialization of its bars and actions, written atomically and never modified. DuckDB queries
it; Redis caches query results keyed by the version hash.
**Consequences.** Caches never need invalidation; experiments reference an exact version;
tampering is detectable with `DatasetStore.verify`.

## ADR-0005 Next-session execution, sells before buys, shrink-to-fit buys

**Decision.** Orders are decided at the rebalance close and filled at the next session's open
(or close). Sells execute first; buys that exceed available cash are reduced to the affordable
quantity (flagged `partial`); orders with no price on the fill date are rejected and logged.
**Consequences.** No same-bar fills; no negative cash without an explicit margin model.

## ADR-0006 Type checking: strict mypy, pandas treated as untyped

**Context.** `pandas-stubs` produced dozens of false positives on idiomatic DataFrame code.
**Decision.** Keep `mypy --strict` for our own signatures, drop pandas-stubs and disable
`no-any-return`.
**Consequences.** Signature discipline is enforced; DataFrame internals rely on tests.

## ADR-0007 Login throttling: Redis exact, degraded mode fixed hashed shards

**Context.** Credential endpoints must stay throttled across replicas and during a Redis
outage. Successive automated reviews showed that per-key in-process fallbacks with eviction
always trade a bypass (evicting a live counter) against a lockout (shared overflow).
**Decision.** Primary control is a Redis fixed-window counter reserved atomically with
`INCR` before password verification, keyed per client address (all attempts) and per
account (failed attempts only, refunded on success, 3x threshold). In degraded mode each
process uses a fixed table of 4096 counters indexed by a keyed hash (random per-process
secret, so collisions cannot be precomputed): constant memory, O(1), never evicted;
refunds are ignored because a shard may be shared.
**Consequences.** During a Redis outage throttling is per-replica and may over-throttle on
hash collisions, but cannot be reset by flooding. Production deployments should run Redis
with persistence and monitoring (see docs/RUNBOOK.md).

## ADR-0008 Client address handling behind the web proxy

**Context.** The Next.js `/api/*` proxy sits between browsers and the API, and the API
throttles logins and registrations per client address. Next keeps a client-supplied
`X-Forwarded-For` unchanged (it fills the header only when it is absent), and a route
handler cannot read the socket peer, so the web tier alone cannot tell a real client
address from a forged one.
**Decision (revised 2026-10-30, #3).** Only a trusted TLS edge states the client address.
* The edge (Caddy, `infra/edge/`) overwrites `X-Forwarded-For` with its TCP peer, never
  appends, strips `Forwarded`/`X-Real-IP`, and adds a shared secret header. It is the only
  published service; only it and the web tier share a network.
* The web tier, when `QUANTLAB_EDGE_SECRET` is set, refuses (403 `edge_required`) any
  request without the secret and forwards the edge's single address. Without the secret
  (development, no edge) it forwards no address at all.
* The API accepts the header only from `QUANTLAB_TRUSTED_PROXIES` (compose: `web`),
  resolved off the request path with last-good retention (`security.TrustedProxies`).
  A trusted proxy that states no usable address means "unknown" (`security.rate_limit_address`).
  In production that means the edge is missing, and login and registration fail closed
  (503 `edge_required`). In development the per-address limit is skipped (the per-account
  limit still applies) and all registrations share one coarse cap. A shared login bucket
  would have let ten failed logins lock every user out.
**Why revised.** The first rule (forward only with `TRUST_FORWARDED_FOR=true`) made the
API see every browser as the web container, so one client's failed logins returned 429 to
everyone (audit 2026-10-30). Its replacement (forward the rightmost hop) let a client that
reaches the web tier choose its address. The earlier claim here that such a client "cannot
lock anyone else out" was wrong: it could send a victim's address to exhaust the victim's
bucket, and rotate addresses to spend a victim account's failed-login budget, because the
account limit is reserved before the password is checked.
**Consequences.** Public exposure requires three things (docs/RUNBOOK.md, "Exposing a
deployment"): the edge, `infra/edge/verify.sh` passing, and a Docker host that preserves
client source addresses. Docker Desktop, rootless Docker and docker-proxy replace them with
a gateway address. The remote probe in `verify.sh` checks this. IPv6 clients are throttled
per /64. Without the edge, development has no
per-address login throttling, which is acceptable only on 127.0.0.1. A client can still delay one account's
login by spending its failure budget (30 per minute). It needs only about three real
addresses to do so, because each address allows 10 attempts a minute. Addresses can no
longer be forged. Per-device login credentials would fix this; that is future work.
