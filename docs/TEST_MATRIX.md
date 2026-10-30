# Test matrix

| Layer | Location | What it proves |
|---|---|---|
| Unit: calendar | `tests/unit/test_calendar.py` | NYSE holiday rules; session counts match published totals (2021–2024) |
| Unit: data | `tests/unit/test_data.py` | Hand-computed split/dividend adjustment; as-of adjustment uses no future events; timezone normalization; deterministic synthetic data passes quality; injected defects are detected; content addressing and tamper detection; retries/backoff/throttling; resumable ingestion |
| Property-based | `tests/unit/test_ledger.py` | Random operation sequences never produce negative cash/positions; replay reproduces state; cash equals the sum of cash flows |
| Financial golden | `tests/unit/test_analytics.py` | Returns, CAGR, volatility, Sharpe (incl. risk-free convention), Sortino, drawdown episodes, beta/alpha/TE/IR, FIFO trade stats, VaR/ES, risk contributions, closed-form min-variance, risk parity budgets, infeasibility, stress, Monte Carlo determinism |
| Engine correctness | `tests/unit/test_backtest.py` | Zero-cost buy-and-hold equals exact total return; determinism; daily reconciliation for every strategy; constraints; costs reduce wealth; strategies never see future bars; **decisions invariant to truncating future data**; next-open fills; slippage/commission math; split between decision and fill; dividends; delisting; rejections |
| Research validation | `tests/unit/test_research.py`, `tests/integration/test_research_api.py` | Grid validity, chronological folds with embargo, walk-forward selects by in-sample Sharpe and tests later windows, monotone cost sweep, PSR/DSR properties, bootstrap determinism, regime partitions, **leakage detector catches a planted adjustment look-ahead**, integrity statuses, validation job via API and integrity before/after validation and reproduction |
| AI evaluation | `tests/unit/test_assistant.py`, `quantlab.assistant.evals`, `tests/integration/test_research_api.py` | 14-question suite: numerical consistency, citation grounding, advice refusal, out-of-scope handling; hallucinating, advice-giving, malformed and failing providers never reach the user; advice questions never reach the model; assistant numbers equal analytics numbers |
| Reports | `tests/integration/test_research_api.py` | HTML/Markdown export from persisted results, script-free, CSP, escaping of user content, fingerprint cited |
| API contract | `tests/integration/test_api_workflow.py` | OpenAPI, health/readiness, metrics, catalog and bars (cache hit, adjusted), full backtest workflow, P&L attribution reconciles to equity, reproduction fingerprint match, manifests, comparison, idempotency and conflicts, structured validation errors, saved strategies, cancellation, audit |
| Security | `tests/integration/test_security.py` | Auth flows, cookie flags, forged/none-alg/expired tokens, logout-everywhere revocation, role enforcement, last-owner protection, rate limits (per IP, per account failures, atomic reserve, degraded mode), injection-like inputs, security headers, production config guards, metrics token, dataset size limits, cross-workspace isolation |
| Failure injection | `tests/integration/test_jobs_reliability.py` | Duplicate delivery → one result; lease expiry + takeover → stale worker fenced out; reaper requeue and exhaustion; transient retries; permanent failures; cancellation while running and queued; timeouts; no partial results on failure; Redis outage → no lost jobs |
| Migrations | `tests/integration/test_migrations.py` | Downgrade/upgrade round trip; no drift between models and migrations; single head |
| Browser E2E | `apps/web/e2e/` | Sign-up, synthetic ingestion, strategy builder, async backtest, results explorer, reproduction match; login errors; auth redirects (desktop + mobile) |

Not yet covered (tracked in ROADMAP.md): performance regression tests, load tests,
hosted-model AI evaluation (no credentials).
