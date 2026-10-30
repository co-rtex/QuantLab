# Status (authoritative handoff)

_Last updated: 2026-10-30_

## Current phase
Phases 0–6 complete; Phase 8 assistant and report export complete. **Next: benchmarks
harness, ML pipeline, portfolio workspace UI, optimization/stress UI, case study.**

## Latest verified milestone
ML research pipeline (point-in-time features, purged/embargoed
chronological CV, baselines vs logistic/RF/GBM, IC and quintile spreads; API, UI, E2E),
benchmark harness with measured results (docs/BENCHMARKS.md), grounded research assistant (deterministic engine + optional Claude phrasing with
numeric verification; eval suite in docs/AI_EVAL.md) and HTML/Markdown report export.
Before that: research validation and the Research Integrity Report: walk-forward with in-sample
parameter selection, parameter and cost sweeps, stationary-bootstrap CIs, PSR/deflated
Sharpe, regime breakdown and an empirical look-ahead check (proven to catch a planted bug),
run as a background job and rendered in the experiment page. Playwright 10/10 (workflow now includes validation, assistant and report export).

## Branch / commits
`main`, pushed to `origin` (github.com/co-rtex/QuantLab). See `git log`.

## Tests last executed (2026-10-30)
* Python: 117 tests (unit, property-based, golden, research, integration, security,
  migrations, failure injection) passing.
* Web: `eslint`, `tsc --noEmit`, `next build` passing; Playwright 10/10 against a local
  stack (workflow incl. validation and reproduction, auth, proxy and redirect security).
* CI (GitHub Actions): python, web, docker jobs green on the last checked run.
* Lint: ruff clean; mypy --strict clean.

## In progress / immediate next steps
1. Address findings from the fresh-context review of the financial core (in progress).
2. Case study write-up (docs/CASE_STUDY.md) using the full workflow.
3. Observability: OpenTelemetry tracing, Grafana dashboard, runbook; Terraform (validated).
4. Portfolio workspace (manual ledger + valuation), optimization/stress UI.

## Known blockers
No LLM API key, no market-data API key, no cloud credentials (docs/BLOCKERS.md).

## Local run notes
Dev processes log to `logs/` (gitignored). On macOS the RQ worker needs
`OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` (set by `make worker`). If `next start` serves
404s for chunks after a rebuild, kill the old server on :3000.
