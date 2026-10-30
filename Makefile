.PHONY: setup lint format typecheck test test-unit test-integration coverage up down app-up web-dev api-dev worker reaper bench e2e check stack-env

# Per-checkout compose project, ports and database URLs (see scripts/stack-env.sh), so
# several git worktrees can run their stacks and test suites at the same time.
$(foreach kv,$(shell scripts/stack-env.sh),$(eval export $(kv)))

stack-env:
	@scripts/stack-env.sh

setup:
	uv sync
	cd apps/web && npm ci

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy

test-unit:
	uv run pytest tests/unit tests/golden -q

test-integration: up
	uv run pytest tests/integration -q

test: test-unit test-integration

coverage: up
	uv run pytest --cov --cov-report=term-missing:skip-covered --cov-report=xml -q

up:
	docker compose up -d --wait postgres redis

down:
	docker compose down

app-up:
	docker compose --profile app up -d --build --wait

api-dev: up
	uv run alembic -c services/api/alembic.ini upgrade head
	uv run uvicorn quantlab_api.main:app --reload --port $(QUANTLAB_API_PORT)

worker:
	OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES uv run quantlab-worker

reaper:
	uv run quantlab-cli reaper --interval 15

web-dev:
	cd apps/web && API_INTERNAL_URL=http://localhost:$(QUANTLAB_API_PORT) npx next dev -p $(QUANTLAB_WEB_PORT)

bench:
	uv run python benchmarks/run_all.py

e2e:
	cd apps/web && npx playwright test

check: lint typecheck test
