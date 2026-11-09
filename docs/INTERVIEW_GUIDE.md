# Interview guide

Notes for explaining QuantLab in a technical interview: what it is, why it is built this
way, what was hard, and what it does not do. Every claim below points to where the
repository shows it, so you can open the evidence while you talk.

## The two-minute version

QuantLab is a self-hosted research platform for equity strategies. You version market
data, define a strategy, and run a backtest as a background job. You then get performance,
risk, out-of-sample validation and an integrity report that says what the result does and
doesn't support. Any result can be re-run and compared byte for byte.

The interesting part isn't the strategies; it is making the results trustworthy:
* no look-ahead, no survivorship;
* no fills that couldn't have happened;
* undefined statistics reported as undefined;
* background jobs that survive crashes;
* all of it on a machine with under 4 GiB for containers.

The bundled data is synthetic and labelled so everywhere. **No result in it says anything
about real markets.**

## How it was built (say this plainly)

QuantLab was developed with AI coding agents working under my direction. The repository
records how: `docs/AUTONOMY.md`, `docs/COORDINATION.md`, `docs/agents/`, and the reviews
and audits. Be ready to describe your own role accurately: the requirements, the decisions
you made or approved, what you reviewed, and what you understand. An interviewer will judge
you on the last one. The rest of this guide is for building that understanding: each
section says where to look.

## Technology choices, and why

| Choice | Why | Trade-off accepted |
|---|---|---|
| A Python domain library with no web or DB imports (`packages/quantlab`) | Every number is computed by pure functions, testable without services | Wiring code lives in the service, and adds a layer |
| FastAPI + Pydantic | Typed request validation and OpenAPI for free; async isn't needed, since heavy work runs in jobs | |
| PostgreSQL as the source of truth for jobs (ADR-0002) | Transactions, constraints and the database's own clock make leases, fencing and idempotency checkable | More logic than a hosted queue's at-least-once delivery |
| Redis + RQ for delivery only | Simple fork-per-job workers; Redis holds no state that matters | RQ's internals are pinned and partly overridden (`pyproject.toml`) |
| Immutable Parquet versions, read with DuckDB (ADR-0004) | Content-addressed data makes "same inputs" provable; row-group statistics let loads skip data | Files on a volume, not in object storage |
| Raw prices stored, adjusted point-in-time (ADR-0003) | Adjustment as known at each date; no stored series that already "knows" later splits | Adjustment is computed on every read |
| Event-driven daily engine (ADR-0005) | Partial fills, cash limits, rejections, delistings and next-session execution are explicit | Slower than a vectorized backtest; daily bars only |
| Next.js web through a same-origin `/api` proxy | The session cookie stays first-party; one origin to secure | The proxy has to handle body limits and forwarded addresses carefully (ADR-0008) |

## Architectural trade-offs worth discussing
* **A modular monolith plus workers, not microservices** (ADR-0001). One deployable API and
  one worker image; the domain boundary is enforced by imports, not by the network.
* **Correctness over speed in the engine.** The engine is cross-checked against a slower
  reference implementation, and every behaviour change bumps an engine version that is
  part of each experiment's input hash.
* **Fail closed.**
  * Undefined statistics are `None` with a reason.
  * A failed optimization is an error, unless it is certified optimal.
  * Production refuses an untrusted proxy.
  * Playwright refuses to aim at another checkout's stack.

## The hardest bugs
See [ENGINEERING_CASE_STUDY.md](ENGINEERING_CASE_STUDY.md). Good ones to tell:
* **A fabricated 50% loss:** a split on a non-session date was silently dropped (§2).
* **Zero-volume fills:** a backtest could escape a halt at a price nobody could trade
  (§3).
* **An optimizer that reported failure at the optimum:** the fix is a mathematical
  certificate, not a looser tolerance (§6).
* **A look-ahead check that missed the instrument master:** with a subtle exemption for
  halted holdings (§1).

## How financial correctness was validated
1. **Hand-computed cases** (`tests/unit/test_research_correctness.py`): backtests checked
   against arithmetic you can redo on paper.
2. **An independent reference backtester** (`tests/oracle/reference.py`), written from
   `docs/METHODOLOGY.md` and importing nothing from the engine. It agrees to 1e-9 on
   seeded random universes.
3. **Mutation testing** (`tests/oracle/mutate.py`): 36 injected defects, each caught by
   some suite. The table in `docs/TEST_MATRIX.md` says which.
4. **A/B records** (`tests/oracle/ab.py`): every engine change compared with the previous
   engine on 1,002 runs, and every changed run explained.
5. **Look-ahead checks**, at the strategy level and at the level of the whole engine.

Be ready for: *"How do you know the reference isn't wrong in the same way?"* Two
answers:
* It was written from the documented method, not from the engine's code.
* Where the two still share an assumption, `TEST_MATRIX.md` lists it, for example the
  affordability cut. Mutation testing shows the suites detect real defects.

## Recovery and failure handling
* **A worker dies mid-job:** its lease expires (database time), the reaper requeues the
  job, and the attempt number fences the dead attempt's late writes.
* **Redis is down:** jobs already running continue. The worker outlasts the outage. Login
  throttling falls back to a bounded per-process limit (ADR-0007).
* **Out of memory:** each container has a limit, and the VM minimum is their sum.
  Admission control refuses work that won't fit, with a 503 and `Retry-After`.
* **Details:** `docs/RUNBOOK.md`.

## What would change at higher scale
These are design directions, not things the project has measured.
* **Object storage** (S3) for dataset versions and artifacts instead of a shared volume.
  The Terraform sketches EFS (ADR-0012).
* **A managed queue, or Postgres-native job claiming** (`SKIP LOCKED`), instead of RQ, and
  autoscaled workers by queue depth.
* **Vectorized engine paths** for the common cases, keeping the event-driven engine as the
  reference.
* **Read replicas, and partitioned job and result tables** as history grows.
* **Load testing from a separate host.** It has not been done (#20).

## What real market data and production would need
* **A licensed vendor** behind the existing provider interface. The adapter exists as a
  fixture-tested contract and needs credentials (#11). Plus a reconciliation report
  against a second source (#12).
* **Public exposure:** the TLS edge verified on an address-preserving host from a second
  machine (#39), and an account lifecycle: password reset, admin (#168).
* **Secrets, backups and monitoring in a real environment.** The Terraform is validated and
  has never been applied.
* **A hosted LLM:** evaluated with a key and an independent judge (#15). By default the
  assistant is deterministic and citation-checked.

## Questions to expect, with honest answers
* **"Does the momentum strategy work?"** On this data, the question doesn't apply: the data
  is synthetic. The integrity report on the demo run shows a Sharpe interval that includes
  zero and an out-of-sample decay, and says so.
* **"Why not a vectorized backtest?"** Partial fills, cash limits and halts are sequential
  by nature. Vectorized code can approximate them, but then it needs its own reference.
* **"What isn't done?"** See the limitations in the README and
  `docs/RELEASE_READINESS.md`. The honest list is short and specific.

## Demonstrating it live
Follow [DEMO.md](DEMO.md). It seeds a workspace with one command, then walks through the
features in an order that tells the story above.
