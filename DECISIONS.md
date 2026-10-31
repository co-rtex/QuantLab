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
per-address login throttling, which is acceptable only on 127.0.0.1.
**Amendment (2026-10-31, #55): device cookies.** About three real addresses could spend an
account's failure budget (30 per minute) and hold its owner at 429. A browser that signs in
successfully now receives a device cookie. A later login that presents a valid one for the
submitted email is throttled per device (10 failures an hour) instead of by the account
budget, so the owner's own browsers keep working during such an attack.
* The cookie selects a throttling budget only; it never authenticates.
* It is stateless and signed with HMAC-SHA256 under a key derived from the JWT secret by
  HKDF with its own context label, never the raw JWT key. It binds a hash of the
  normalized email, the issue time and the account's token version, and holds a random
  device id. Validation is constant-time; the maximum age is 90 days; logging out
  everywhere (a token version bump) revokes every device cookie.
* Attributes: HttpOnly, SameSite=Strict, Path=/api/v1/auth, Secure with the `__Secure-`
  name prefix over TLS. Not `__Host-`, which requires Path=/.
* After 10 failures in an hour a device loses its exemption and falls back to the account
  budget. Without a valid cookie for that email nothing changes.
Residual: a victim's *new* device can still be held at 429 by a distributed attacker.
A progressive delay instead of a hard lock was rejected: an attacker can hold the delay at
its maximum, and slow responses tie up workers.

## ADR-0010 Registration reveals whether an email has an account (accepted risk, for now)

**Context.** `POST /auth/register` answers 409 `email_taken` for an existing address, so it
tells anyone whether an address has an account. Login (dummy Argon2 hash) and invitations
(#5) do not. The standard fix is a uniform "check your email" response with email
verification, and that needs a way to deliver mail.
**Decision (2026-10-31, #55).** Keep the 409 for now (option B). Revisit with option A,
uniform registration plus email verification through the `Notifier` interface, once a real
email adapter exists; the lead takes A to the user as a product decision.
**Why.** Without a mail adapter, option A has no production delivery path: it needs a
development-only token outbox, the kind of plaintext store removed from #37. It would also
change every sign-up flow and test.
**Bounds of the accepted risk.** Behind the TLS edge, registrations are limited to 10 per
minute per real client address (IPv6 per /64), and production refuses registrations
without a known client address (ADR-0008, #39). Each probe that finds a free address
creates an account, which is visible in the audit log (`user.register`).
