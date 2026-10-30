# Roadmap

Dependency-ordered. ✅ done and verified · 🔶 partial · ⬜ not started.

| Phase | Item | State |
|---|---|---|
| 0 | Preflight, architecture, project memory | ✅ |
| 1 | Workspace, tooling, Docker Compose, FastAPI, Next.js, migrations, CI | ✅ |
| 2 | Calendar, schema, synthetic provider, quality checks, versioned store, ingestion with throttling/retries/checkpoints, bar queries + cache | ✅ |
| 2 | CSV import endpoint (provider exists in domain) | ⬜ |
| 3 | Ledger with invariants, corporate actions | ✅ |
| 3 | Portfolio workspace API + UI (manual transactions, valuation history) | ⬜ |
| 4 | Strategy framework (9 strategies), event-driven engine, async jobs, results explorer | ✅ |
| 5 | Performance, risk, drawdowns, attribution, cost analysis, visualizations | ✅ |
| 5 | Factor exposures / regression on benchmark and sector factors | ⬜ |
| 6 | Experiment tracking, input hashes, reproduction, manifests | ✅ |
| 6 | Walk-forward, sensitivity, cost sweeps, bootstrap CIs, deflated Sharpe, regimes, leakage check | ✅ |
| 6 | Research Integrity Report (13 checks; API + UI) | ✅ |
| 7 | Optimizers (domain) | ✅ |
| 7 | Optimization / stress / Monte Carlo API + UI | ⬜ |
| 8 | ML pipeline (purged chronological CV, baselines, artifacts; API + UI) | ✅ |
| 8 | Grounded research assistant + deterministic fallback + eval suite | ✅ (hosted-model eval pending credentials) |
| 8 | Report export (HTML/Markdown) | ✅ |
| 9 | OpenTelemetry tracing, Grafana dashboards, runbook, Terraform (validated, not applied) | ⬜ |
| 9 | Benchmarks harness and BENCHMARKS.md measurements | ✅ (load testing and worker scaling pending) |
| 10 | Case study: momentum vs passive after costs, out of sample (docs/CASE_STUDY.md) | ✅ |
| 10 | Fresh-context financial-core review (10 findings, fixed with regression tests) | ✅ |
| 10 | Accessibility audit, architecture review of service layer | ⬜ |
