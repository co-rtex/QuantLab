# Status (authoritative handoff)

_Last updated: 2026-10-30_

## Current phase
Phases 0–5 complete with verified vertical slice; **next: Phase 6 (validation and
Research Integrity Report)**.

## Latest verified milestone
Full stack runs locally: synthetic ingestion → async backtest via RQ worker → results
explorer → reproduction with fingerprint match, verified by Playwright E2E (5/5 passing).

## Branch / commits
`main`, pushed to `origin` (github.com/co-rtex/QuantLab). See `git log`.

## Tests last executed (2026-10-30)
* Python: 92 tests (unit, property-based, golden, integration, security, migrations,
  failure injection) passing; coverage 86% (domain + API).
* Web: `eslint`, `tsc --noEmit`, `next build` passing; Playwright 5/5 against a local stack.
* Lint: ruff clean; mypy --strict clean.

## In progress / immediate next steps
1. `quantlab.research`: chronological splits, walk-forward, parameter and cost sensitivity,
   bootstrap CIs, deflated Sharpe, regime analysis, empirical leakage check.
2. Research Integrity Report (domain + API + UI) with verified / warning / unassessed checks.
3. Benchmarks harness (`benchmarks/`) with recorded hardware and measurements.
4. Portfolio workspace (manual ledger + valuation) API and UI.
5. ML pipeline, grounded assistant with deterministic fallback and eval suite, report export.

## Known blockers
No LLM API key, no market-data API key, no cloud credentials (docs/BLOCKERS.md).

## Local run notes
Dev processes log to `logs/` (gitignored). On macOS the RQ worker needs
`OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` (set by `make worker`). If `next start` serves
404s for chunks after a rebuild, kill the old server on :3000.
