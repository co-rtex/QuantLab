# Status (authoritative handoff)

_Last updated: 2026-10-30_

## Current phase
Phases 0–6 complete. **Next: benchmarks harness, portfolio workspace UI, then Phase 7–8
(optimization/stress UI, ML pipeline, grounded assistant, report export).**

## Latest verified milestone
Research validation and the Research Integrity Report: walk-forward with in-sample
parameter selection, parameter and cost sweeps, stationary-bootstrap CIs, PSR/deflated
Sharpe, regime breakdown and an empirical look-ahead check (proven to catch a planted bug),
run as a background job and rendered in the experiment page. Playwright 10/10.

## Branch / commits
`main`, pushed to `origin` (github.com/co-rtex/QuantLab). See `git log`.

## Tests last executed (2026-10-30)
* Python: 103 tests (unit, property-based, golden, research, integration, security,
  migrations, failure injection) passing.
* Web: `eslint`, `tsc --noEmit`, `next build` passing; Playwright 10/10 against a local
  stack (workflow incl. validation and reproduction, auth, proxy and redirect security).
* CI (GitHub Actions): python, web, docker jobs green on the last checked run.
* Lint: ruff clean; mypy --strict clean.

## In progress / immediate next steps
1. Benchmarks harness (`benchmarks/`) with recorded hardware and measurements.
2. Report export (HTML/Markdown) from persisted results + integrity report.
3. Grounded research assistant with deterministic fallback and evaluation suite.
4. ML pipeline (purged chronological CV, baselines, artifacts).
5. Portfolio workspace (manual ledger + valuation), optimization/stress UI.
6. Case study: momentum vs passive after costs, out of sample.

## Known blockers
No LLM API key, no market-data API key, no cloud credentials (docs/BLOCKERS.md).

## Local run notes
Dev processes log to `logs/` (gitignored). On macOS the RQ worker needs
`OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` (set by `make worker`). If `next start` serves
404s for chunks after a rebuild, kill the old server on :3000.
