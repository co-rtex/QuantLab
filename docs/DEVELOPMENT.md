# Development

How to work on QuantLab from source: the processes it runs, several checkouts at once, and
the test layers. The README has the short version.

## Requirements
* Docker. The full stack (`make app-up`, 2 workers) needs at least **5 GiB of Docker VM
  memory**: the sum of its memory limits (2 workers at 1,536 MiB and the API at 1,880 MiB)
  plus the measured peaks of Postgres, Redis, web and the reaper, about 110 MiB, which is
  5,062 MiB (#175, #220; [RUNBOOK, Resources](RUNBOOK.md#resources)). On a smaller VM, run
  one worker: `QUANTLAB_WORKERS=1 make app-up` (1,536 + 1,880 + about 110 = 3,526 MiB). `make` warns when the
  VM is under the minimum.
* Python 3.12 with [uv](https://docs.astral.sh/uv/), and Node.js 22.

## Running the processes yourself

```bash
make setup            # uv sync + npm ci
make up               # PostgreSQL 17 and Redis 7 (ports 5433, 6380 on localhost)
make api-dev          # migrations + API on :8000   (terminal 1)
make worker           # background worker            (terminal 2)
make reaper           # stale-job recovery           (terminal 3)
make web-dev          # frontend on :3000            (terminal 4)
```

Or everything in containers: `make app-up` (web on :3000, API on :8000, two workers).

* **API docs:** http://localhost:8000/api/v1/docs. They are served only with
  `QUANTLAB_ENVIRONMENT=development` (the default), and never through the web app's `/api`
  proxy (#201).
* **On macOS** the RQ worker needs `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`, which
  `make worker` sets.
* **The code version** each run records is the commit `scripts/stack-env.sh` emits, with
  `-dirty` for uncommitted changes. Images get it as a build argument (see METHODOLOGY,
  *Code version*).

## Several git worktrees at once

The ports above are the main checkout's. In a linked worktree, `make` gives the checkout
its own Compose project, volumes and port block (`make stack-env` prints them; override
with `QUANTLAB_SLOT=<1-89>`), so `make up`, `make app-up`, `make api-dev`, `make web-dev`
and `make e2e` never touch another checkout's containers. In a linked worktree, `make`
(through `scripts/stack-env.sh`) also writes a gitignored `docker-compose.override.yml`
naming that project, so a bare `docker compose` there never reaches the main checkout's
`quantlab` stack (#233). Two exceptions: before a worktree's first `make`, and with an
explicit `-f` list, which skips override files; pass `-p` then. Integration tests use a
per-worktree database (`quantlab_test_<hash>`) and a session advisory lock, so concurrent
runs that do share a database wait for each other instead of truncating each other's tables.

Full-stack runs (`make e2e-serial`, or any command under `scripts/fullstack-lock.sh run`)
take a lock in git's common directory, which every linked worktree of one clone shares. A
**separate clone** has its own git directory, and so its own lock: it does not wait for
this clone's runs. Stop its stacks before running one here, or run from a worktree of the
same clone. A separate clone also defaults to slot 0, the `quantlab` project; give it its
own `QUANTLAB_SLOT` (#233).

## Tests

```bash
make lint typecheck   # ruff + mypy --strict
make test-unit        # domain tests (golden, property-based, look-ahead checks)
make test-integration # API, security, migrations, job failure injection (needs `make up`)
make coverage
make e2e-serial       # full stack + Playwright, under the full-stack lock (one worker)
cd apps/web && npm run lint && npm run typecheck && npm test   # web unit tests
uv run python -m tests.oracle.mutate   # inject engine defects; the suites must catch each
```

* **E2E** runs only through `make e2e-serial`. In a linked worktree, Playwright refuses an
  unset `E2E_BASE_URL`, or the main checkout's `:3000`, unless `E2E_ALLOW_MAIN_CHECKOUT=1`
  (#263).
* **What each layer covers:** [TEST_MATRIX.md](TEST_MATRIX.md).

## Continuous integration
`.github/workflows/ci.yml` runs on every push to main and every pull request.
* **`python`:** ruff, mypy, and every test outside `tests/integration`.
* **`python-integration`:** `tests/integration` against a Postgres service. The two run in
  parallel.
* **`coverage`:** combines both jobs' coverage into one report and `coverage.xml`.
* **`audit`:** pip-audit on the runtime dependencies (blocking) and the dev tools (report
  only), independent of the tests, so a slow test run never skips it.
* **`web`:** eslint, `tsc`, unit tests, a production build, and npm audit (blocking for
  runtime dependencies, report-only for dev tools).
* **`docker`:** builds the API image (the web image is built by the E2E workflow).

`.github/workflows/e2e.yml` runs the Playwright suite against a full compose stack on pull
requests that touch the web, API, library or compose files.
