# QuantLab — working instructions

Read `STATUS.md` first (authoritative handoff), then `ROADMAP.md`. Verify `git status` and
`git log --oneline -10` before writing anything.

## Layout
- `packages/quantlab/` — pure domain library (data, ledger, backtest, analytics, research, ml, assistant). No web/DB imports.
- `services/api/` — FastAPI app, SQLAlchemy models, Alembic migrations, RQ worker (`quantlab_api`).
- `apps/web/` — Next.js frontend (talks to API through `/api` rewrite).
- `tests/` — `unit/` (no services), `integration/` (needs Postgres/Redis), `golden/`.
- `docs/` — architecture, methodology, ADRs (`docs/adr/`), benchmarks, blockers.

## Commands
- `make setup` · `make lint` · `make typecheck` · `make test` · `make test-integration` · `make up` (docker compose)
- Python: `uv run pytest tests/unit -q`; web: `cd apps/web && npm run lint && npm run build`.

## Rules
- Financial correctness first: never let strategies see data dated after the decision close; never fill at a price unknown at decision time.
- Undefined statistics are `None` + reason, never 0.
- Synthetic data must always be labelled synthetic.
- Conventional commits; **no AI attribution trailers/footers anywhere**; keep the configured Git identity.
- Never commit secrets or `var/` data. Never force-push.
- Update `STATUS.md` after each milestone.
