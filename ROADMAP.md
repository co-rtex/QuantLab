# Roadmap

Dependency-ordered. ✅ done and verified · 🔶 partial · ⬜ not started.

| Phase | Item | State |
|---|---|---|
| 0 | Preflight, architecture, project memory | ✅ |
| 1 | Workspace, tooling, Docker Compose, FastAPI, Next.js, migrations, CI; `make app-up` verified from clean images with E2E | ✅ |
| 2 | Calendar, schema, synthetic provider, quality checks, versioned store, ingestion with throttling/retries/checkpoints, bar queries + cache | ✅ |
| 2 | CSV import endpoint (provenance, licence, size limits) | ✅ |
| 3 | Ledger with invariants, corporate actions | ✅ |
| 3 | Portfolio workspace API + UI (append-only ledger, valuation, TWR, risk, stress, Monte Carlo) | ✅ |
| 4 | Strategy framework (9 strategies), event-driven engine, async jobs, results explorer | ✅ |
| 5 | Performance, risk, drawdowns, attribution, cost analysis, visualizations | ✅ |
| 5 | Factor exposures: market and sector factors, Newey-West OLS, in analysis and UI | ✅ |
| 6 | Experiment tracking, input hashes, reproduction, manifests | ✅ |
| 6 | Walk-forward, sensitivity, cost sweeps, bootstrap CIs, deflated Sharpe, regimes, leakage check | ✅ |
| 6 | Research Integrity Report (13 checks; API + UI) | ✅ |
| 7 | Optimizers (domain) | ✅ |
| 7 | Stress / Monte Carlo / risk contributions in portfolio UI | ✅ |
| 7 | Portfolio construction research: equal weight, min-variance, mean-variance, risk parity (point-in-time, API + UI) | ✅ |
| 8 | ML pipeline (purged chronological CV, baselines, artifacts; API + UI) | ✅ |
| 8 | Grounded research assistant + deterministic fallback + eval suite | ✅ (hosted-model eval pending credentials) |
| 8 | Report export (HTML/Markdown) | ✅ |
| 9 | OpenTelemetry tracing (request → job, verified in Jaeger), Prometheus/Grafana dashboard, runbook | ✅ |
| 9 | Terraform for a cloud deployment (validated, not applied) | ⬜ |
| 9 | Benchmarks harness: engine, retrieval, cache, API, concurrent load, worker scaling | ✅ |
| 10 | Case study: momentum vs passive after costs, out of sample (docs/CASE_STUDY.md) | ✅ |
| 10 | Fresh-context financial-core review (10 findings, fixed with regression tests) | ✅ |
| 10 | Accessibility: axe WCAG 2.1 AA audit in E2E, zero violations on 11 pages | ✅ |
| 10 | Independent service-layer review (9 findings fixed with regression tests) | ✅ |
| 10 | Independent audit (docs/AUDIT_2026-10-30.md): 18 defects fixed with regression tests; per-worktree stacks and test databases | ✅ |
| 11 | Audit backlog P0: per-user job quota, auth before multipart parsing, invitations, licensed real-data adapter | ⬜ |
| 11 | Audit backlog P1: hosted-LLM eval with independent judge, Terraform, external load test, idempotency phantom rows, CSV retry, storage cleanup, frontend error surfacing, DSR inputs | ⬜ |
