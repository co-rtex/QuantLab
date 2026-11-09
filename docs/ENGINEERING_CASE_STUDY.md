# Engineering case study

Seven problems that were hard to get right in QuantLab: why each was hard, what was built,
and how it was checked. Every figure comes from a test, a measurement in the repository, or
an issue or PR that records it; the reference is given each time. The data throughout is
synthetic: this is about the correctness of the machinery, not about any trading result.

## 1. A backtest must not see the future

**The problem.** A decision on day *t* may use only what was public at the close of *t*.
Leaks are easy to write and hard to see:
* a rolling window that includes tomorrow's bar;
* a split adjustment computed with factors from later splits;
* a universe that knows which stocks later delist.

Each makes a backtest look better and raises no error.

**Why it was hard.** The first look-ahead tests could not detect a one-bar leak. Their
cut-off left the next bar in place, and a mutation leaking one future row of returns passed
every test (docs/AUDIT_2026-10-30.md, finding 9).

**What was built.**
* **Point-in-time views.** The engine gives each strategy a view built only from bars dated
  up to *t*, with prices adjusted as of *t*.
* **An empirical check at the level of the whole engine** (`leakage_check`). It re-runs the
  experiment on data truncated one session after sampled decision dates, and requires
  every decision up to each cut to match the full run.
* **The instrument master as of the cut** (#247, #259). Delistings also live in the master,
  so the truncated run now gets the master as it was knowable then.
  * One exemption had to be argued carefully. A halted, held stock is kept until its
    delisting (CRSP's convention), so the truncated run may know that the delisting
    exists, but not its date or return.
  * Splits, ended halts and NaN prices each needed their own case.
* **The kept session** comes from the run's own calendar (#262). A single-name universe
  that skipped a session was falsely reported as leaking.
* **Versioning.** The validation suite is versioned, so a verdict computed under an older,
  weaker check is never returned as current (suite version 3).

**How it was verified.**
* A future-perturbation test for all nine strategies.
* Mutations M1 (a one-bar look-ahead in the strategy view), M34 and M35 (the halt
  exemption removed, and a delisting return used 10 days early), each caught
  (`tests/oracle/mutate.py`).
* A planted engine leak, acting on each delisting 10 days before it is public, is reported
  "failed". With the old full master, the same leak was "verified"
  (`tests/unit/test_leakage_master.py`).

## 2. Corporate actions, identity and delistings

**The problem.** Prices must be adjusted for splits and dividends as they were known at the
time, positions must split with the shares, a ticker can be reused by a different company,
and a delisted stock's last value is not its last close.

**Why it was hard.** The 2026-10-30 audit found that a corporate action whose ex-date was
not a row of the price matrix (a non-session date, or a session filtered out of the
universe) was **silently dropped**. The ledger never split the shares while the price
halved, so a flat position showed a **fabricated 50% loss** (docs/AUDIT_2026-10-30.md,
finding 1).

**What was built.**
* **Raw prices are stored;** adjustment is derived point-in-time (DECISIONS.md, ADR-0003).
* **Actions are placed on the next bar** when their ex-date has none, and the placement is
  counted and reported.
* **Ingestion gates** reject impossible actions (#10).
* **Permanent instrument ids** key every job (#9), so a reused ticker never merges two
  companies' histories.
* **Delisting returns** come from an instrument master, applied on the session after the
  delisting is public (#49).

**How it was verified.**
* The audit's case (a 2-for-1 split, flat price) now ends at 1000 instead of 500.
* An independent reference backtester (`tests/oracle/reference.py`), written from the
  methodology and importing nothing from the engine, agrees to 1e-9 on 50 seeded universes
  (200 in the slow mode). They combine in-gap and weekend ex-dates, reverse splits, ticker
  reuse and every delisting kind.

## 3. Fills that could not have happened

**The problem.** Some data vendors keep printing a halted stock's last price with zero
volume. A backtest that fills there lets a strategy escape a halt at a price nobody could
trade.

**What was built** (#149, engine 1.4.0).
* A zero-volume bar is not tradable: an order meeting one is rejected (`no_volume`).
* The strategy is still offered the name, so a held position rides through the halt and
  takes the reopen move, rather than being sold and rebought.
* Stale-mark exposure is reported.

**How it was verified.** In the review fixture, a halted name reopened 30% lower.
* **The old engine** let the backtest sell during the halt, and kept 1000.
* **The fixed engine** ends at 700, which is what a holder could actually realize.

A second independent review found that the first version force-sold held names on
zero-volume rebalance days. That was fixed before merge (STATUS.md, #150).

## 4. Background jobs that survive crashes

**The problem.**
* Backtests, validations and ML runs take seconds to minutes. They run as jobs.
* A job must never run twice concurrently, be lost when a worker dies, or be overwritten
  by a stale attempt.
* Two submissions must not block each other.

**Why it was hard.**
* Concurrent submissions in one workspace **deadlocked**: 10 of 12 returned 500
  (docs/AUDIT_2026-10-30.md, finding 3).
* Host clocks can disagree, so a lease checked against a worker's clock could expire
  early or late (#130).

**What was built.**
* **PostgreSQL is the source of truth;** Redis/RQ only delivers job ids (ADR-0002).
* **A worker claims a job** with an atomic conditional update. The attempt number is a
  fencing token, so a stale attempt can't write.
* **Leases, heartbeats, backoff and deadlines** use the database's clock, not the host's
  (#132).
* **A reaper** recovers expired leases and redelivers lost messages.
* **The queue carries an allowlisted job name, never a pickled callable** (#122): a
  compromised Redis can't execute code in a worker.
* **Lock order is fixed:** advisory lock, then workspace, then user.

**How it was verified.**
* The deadlock case went from {500: 10, 202: 2} to {202: 12}.
* Failure-injection integration tests: worker kills, Redis outages, a retry delivered
  before its backoff.
* A route-wide authorization matrix: all API routes classified, and ten deliberately broken
  authorization checks each caught (#135).

## 5. Memory on a small machine

**The problem.** The development machine's Docker VM has 3.8 GiB. Multi-year,
hundred-symbol histories, several workers and an API share it.

**Why it was hard.** With no memory limit on the API, 16 concurrent optimization requests
drove the whole VM into a global out-of-memory kill (#223).

**What was built.**
* Every service has a memory limit, and the documented VM minimum is their sum plus
  measured peaks (docs/RUNBOOK.md, *Resources*).
* Admission control refuses a backtest that wouldn't fit a worker.
* Optimization moved from the API into a job (#224).
* Bar loading streams only the Parquet row groups its statistics select (#248, #225).

**How it was verified.** Loading for one optimization went from about 574 to 190 MiB, with
byte-identical outputs (#248). The resource contract was measured: 3 rounds of 2
concurrent backtests at the admission cap all completed, with no OOM kill
(docs/RUNBOOK.md).

## 6. An optimizer that reported failure at the optimum

**The problem.** Mean-variance at the optimize API's defaults, with 100 symbols, failed in
77 of 78 windows on macOS and 78 of 78 on Linux, with SLSQP's "Positive directional
derivative for linesearch" (#253).

**Why it was hard.** The tempting fixes are wrong:
* **Loosening the tolerance** would move every successful solve, and a previously
  measured threshold with them.
* **Falling back to another allocation** would silently answer a different question.

**What was found.** The stopping tolerance (`ftol` 1e-15) is below what double precision
can resolve for the objective, scaled to O(1) over 100 assets. So the solver stalls *at*
the optimum.
* Against an independent reference optimum (an accelerated projected-gradient solver with
  an exact projection), the "failed" points were within 3.2e-14 of the optimal objective.
* With `ftol` 1e-12, all 78 solves succeed.

**What was built** (engine 1.8.0).
* A successful solve is returned exactly as before.
* A failed solve is accepted only if it is feasible and its **Frank-Wolfe gap** is at most
  1e-6. For a convex objective, that gap is a proven upper bound on the distance from the
  optimum.

**How the threshold was chosen.** It was measured on both platforms:
* converged points have gaps of at most 5.56e-8;
* an optimum perturbed by noise of 1e-5 has a gap of at least 2.62e-6.

**How it was verified.** Tests check the certificate against an LP solver and a
closed-form optimum. Mutation M36 (accepting any failed point) is caught. The A/B over
1,002 backtests showed 0 runs changed (`benchmarks/optimizer_certificate.py`,
docs/METHODOLOGY.md, *Optimizers*).

## 7. Reproducibility, and what it does not guarantee

**The problem.** "Reproducible" has to mean something checkable.

**What was built.**
* **Every experiment records** an input hash (dataset version, strategy, parameters,
  configuration, engine version), the code version, and an output fingerprint.
* **Reproduce** re-runs it and compares fingerprints. If the inputs differ, it names each
  differing input, for example an engine upgrade.
* **Every engine change gets an A/B** over 1,002 runs, with each changed run attributed.

**How it was verified.**
* **Within one platform:** outputs are byte-identical across processes, hash seeds,
  thread counts and restarts (`tests/unit/test_reproducibility_processes.py`).
* **Across platforms** (macOS with Accelerate against Linux with OpenBLAS), optimizer
  outputs agree only at float level: 148 floats differed, by at most 2.22e-15. The
  methodology says so rather than claiming more.
* **The code version** was recorded as "unknown" for every containerized run, and two
  "unknown"s compared as the same code. The 2026-11-07 audit found this. Now each image
  records its commit (`-dirty` for uncommitted changes), and an unknown version is never
  "the same".

## Smaller lessons, each with a test now
* **Logs.** A 500 during login wrote the plaintext password to the logs, through traceback
  frame locals (#38).
* **The login limit.** Behind the web proxy, every browser looked like one client. One
  user's failed logins locked out everyone else (docs/AUDIT_2026-10-30.md, finding 2).
* **The answer verifier.** It accepted a number if *any* fact matched its magnitude. "Gained
  39.4%" built from a −39.4% drawdown was shown as verified (finding 11). It now binds each
  number to the fact it cites, sign included.
* **ML survivorship.** The ML panel dropped stocks that delisted within the label's
  horizon: survivorship bias in every ML metric (finding 5). Later, a short halt dropped a
  stock from the panel for up to 252 sessions (#120).
