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
| 11 | Account-wide job and workspace quotas (#2) | ✅ |
| 11 | Post-audit backlog: tracked as GitHub issues, below | 🔶 |

## Post-audit backlog (dependency order)

Owners, file ownership and migration slots: [docs/COORDINATION.md](docs/COORDINATION.md).
`→` means "must merge before". Live status: `gh issue list --label <owner>`.

| P | Issue | Owner | Depends on |
|---|---|---|---|
| P0 | ✅ #3 Trusted TLS edge (#32) | Agent 2 | — |
| P0 | **#39 Security gate**: fail closed on an untrusted peer, exact-match remote probe, verified from a second machine on an address-preserving host (**no public exposure until closed**) | Agent 2 + user host | #3 → #19 |
| P0 | ✅ #4 Authenticate before parsing multipart uploads (#28) | Agent 2 | — |
| P0 | ✅ #5 Invitations instead of add-member (#37, migration M1) | Agent 2 | — |
| P0 | #9 Permanent instrument identity: ✅ part A (#31); ✅ part B delisting returns (#49; follow-ups #59); part C catalog (M2 open) | Agent 3 | → #11 |
| P0 | ✅ #10 Corporate-action validation gates (#27) | Agent 3 | — |
| P0 | #12 Reconciliation report: ✅ library (#34); API exposure pending (with #9 C); follow-ups #54 | Agent 3 | — |
| P0 | #11 Vendor adapter: ✅ fixture-only contract (#33); **#50 before live wiring**; live part blocked on credentials | Agent 3 | — |
| P0 | #50 Massive adapter correctness before live wiring | Agent 3 | — |
| P1 | ✅ #6 Idempotent retries (#40) | Agent 2 | — |
| P1 | ✅ #43 Idempotency fingerprint (#47) | Agent 2 | — |
| P1 | ✅ #51 CSV ambiguous-commit race (#57) · ✅ #52 synthetic-route idempotency (#61) · #55 lockout/enumeration (ADR approved) | Agent 2 | — |
| P1 | ✅ #7 CSV retry and ambiguous-commit cleanup (#48) | Agent 2 | — |
| P1 | ✅ #13 Undefined-aware statistics (#29) | Agent 3 | — |
| P1 | ✅ #14 Deflated Sharpe input consistency (#30) | Agent 3 | — |
| P1 | ✅ #35 Research-correctness suite (#36) | Agent 3 | — |
| P2 | #41 Review follow-ups for #29, #30, #31, #36 | Agent 3 | — |
| P1 | #16 E2E flake: root cause is the 90 s budget for chained jobs under load; fix #53 | Lead | — → #17 |
| P1 | #17 Playwright E2E in CI | Lead | #16 |
| P1 | #15 Assistant verifier and hosted eval (eval blocked on key) | Unassigned | — |
| P1 | #18 Frontend error surfacing | Unassigned | — |
| P1 | #19 Terraform, validated and never applied | Lead | #3 |
| P1 | #20 External load test (needs a separate host) | Unassigned | — |
| P1 | #21 Storage lifecycle on workspace deletion | Unassigned | #9 |
| P2 | #8 Security hardening: ✅ #38, ✅ A (#64), ✅ C (#65); B (`/ready`) after #62; small follow-up | Agent 2 | #62 |
| P2 | #22 Per-worktree isolation hardening | Lead | — |
| P2 | #23 Frontend completeness and synthetic labels | Unassigned | — |
| P2 | #24 Benchmark methodology, golden tests | Unassigned | — |
| P2 | #25 Job-deletion semantics for results | Lead | — |
