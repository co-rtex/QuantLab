# Multi-agent coordination

How the lead and the implementation agents share this repository without stepping on each
other. GitHub Issues are the source of truth for work state; this file holds the rules,
ownership and current leases. A restarted lead recovers from: this file, `STATUS.md`,
the worker launch briefs in `docs/agents/`, open issues/PRs (`gh issue list`,
`gh pr list`) and `git worktree list`.

## Roles

| Role | Owns | Does not |
|---|---|---|
| **Lead** (integration coordinator) | Review and merge of every PR; shared files below; CI; root docs (`STATUS.md`, `ROADMAP.md`, `PROJECT_SPEC.md`, `docs/ARCHITECTURE.md`, this file); migration slots; cross-domain API contracts | Implement inside a worker's active files |
| **Agent 2 — Security & Backend** | Authentication, authorization, CSV-upload security, invitations, trusted edge / reverse proxy, their tests | Edit market-data, quant or ML code |
| **Agent 3 — Financial Data & Quant** | Providers, permanent instrument identity, corporate actions, reconciliation, point-in-time correctness, statistics, their tests | Edit auth, upload, proxy or edge code |

Work items are issues labelled `owner:*`, `P0`/`P1`/`P2`, `status:*` and `area:*`.
`security-gate` marks issues that prohibit public exposure until closed (#3).

## Module ownership

| Path | Owner |
|---|---|
| `services/api/src/quantlab_api/security.py`, `limits.py`, `uploads.py`, `invitations.py`, `routes/auth.py`, `routes/invitations.py` | Agent 2 |
| `services/api/src/quantlab_api/routes/workspaces.py` (members, invitations) | Agent 2 |
| `services/api/src/quantlab_api/routes/datasets.py` | Agent 3 (since #65; Agent 2's upload work #4, #7, #51, #52 and #8 C is merged) |
| `apps/web/src/app/api/[...path]/route.ts`, `apps/web/src/proxy.ts`, `infra/edge/**` | Agent 2 |
| Web pages for login, settings, members, invitations | Agent 2 |
| `routes/experiments.py` | — (#154 merged) |
| `routes/ml.py` | — (#148 merged) |
| `routes/research.py` | Unassigned (lead leases per issue) |
| `packages/quantlab/src/quantlab/data/**`, `services/api/src/quantlab_api/marketdata.py` | Agent 3 |
| `packages/quantlab/src/quantlab/{backtest,analytics,research,ml,portfolio,optimize,strategies}/**` | Agent 3 |
| `docs/METHODOLOGY.md` | Agent 3 |
| `benchmarks/**` | Agent 3: #24 harness (no published figures until a quiet window); `ml_liquidity_selection.py` (#125) |
| `services/api/src/quantlab_api/observability.py`, `db.py`, `apps/web/playwright.config.ts` | Lead |
| `packages/quantlab/src/quantlab/assistant/**`, `tests/unit/test_assistant.py`, the verifier section of `docs/AI_EVAL.md` | Agent 2 for #15 (leased) |
| `apps/web/src/lib/useApi.ts`, `apps/web/src/lib/workspace.tsx`, `apps/web/src/components/**`, `apps/web/src/app/(app)/**`, new E2E specs | Agent 2 for #18 (leased) |
| Other web pages | Unassigned (lead assigns per issue) |

## Shared files (lead-owned, leased one holder at a time)

A worker edits a shared file only while it holds a lease recorded here. Ask for one by
commenting on the issue; the lead updates this table in the same PR that grants it or in a
follow-up docs commit. A lease ends when the PR that used it merges or closes.

| Shared file | Current holder | For |
|---|---|---|
| `services/api/src/quantlab_api/models.py` | — | (#129 merged) |
| `services/api/src/quantlab_api/schemas.py` | — | (#106 merged) |
| `services/api/src/quantlab_api/config.py` | — | (#128 merged) |
| `services/api/src/quantlab_api/jobs.py` | Agent 2 | #162 unique worker names (then #167); Agent 3 touches only the OOM classification for #163, coordinated with Agent 2 |
| `services/api/src/quantlab_api/tasks.py` | — | (#154, #156 merged) |
| `services/api/src/quantlab_api/limits.py` (module owner: Agent 2) | Agent 3 | #163 admission bound for memory-heavy backtests |
| `services/api/alembic/env.py` | — | (#37 merged) |
| `scripts/case_study.py` | — | (Agent 2's invitation fix merged in #37) |
| `services/api/src/quantlab_api/main.py` | — | (#128 merged) |
| `services/api/alembic/versions/**` | see migration slots | |
| `docker-compose.yml` | Agent 2 | #162 `restart: unless-stopped` on every service |
| `Makefile`, `scripts/stack-env.sh` | — | |
| `pyproject.toml`, `uv.lock`, `apps/web/package*.json` | — | |
| `.github/workflows/**` | lead only | |
| `DECISIONS.md` | — | (#69 merged) |
| `STATUS.md`, `ROADMAP.md`, `PROJECT_SPEC.md`, `docs/ARCHITECTURE.md`, this file | lead only | |

Workers report status in their PR description and issue comments, not in `STATUS.md`.

## Migration slots

Alembic revisions form one linear chain, so only one migration may be in flight at a time.

| Slot | Holder | Issue | State |
|---|---|---|---|
| M1 | Agent 2 | #5 invitations, revision `7c3e5a1d9b20` → `4f1c2a9e7b10` | **merged** (#37, `9e76ff3`) |
| M2 | Agent 3 | #9 part C, revision `9a4c6e2f1b37` → `7c3e5a1d9b20` | **merged** (#62, `925cc1c`) |
| M3 | Agent 3 | #66 revision `b5e1d7c3a924` → `9a4c6e2f1b37`: instruments key `(dataset_version_id, symbol, instrument_id)` `NULLS NOT DISTINCT` | **merged** (#87, `52508ed`) |
| M4 | Agent 2 | #123 revision `4d2b8e6f0c15` → `b5e1d7c3a924`: nullable `jobs.not_before` | **merged** (#129, `db005e4`) |
| M5 | Agent 2 | #167 revision `18869f6a1dae` → `4d2b8e6f0c15`: nullable `jobs.attempt_started_at` | in review (#171) |

Rules: request the slot on the issue before running `alembic revision`; set `down_revision`
to the current head on `main` at the time; include a working `downgrade`; the migration
test must pass. If `main` gains a revision first, the lead re-points `down_revision` during
integration. Migrations run only against your own worktree's database (below), never
against another stack.

## Worktrees, stacks and ports

Each agent works only in its own worktree, branch and Compose project. Slots are fixed
(do not rely on the path hash, which can collide):

| Who | Worktree | `QUANTLAB_SLOT` | API / web / Postgres / Redis |
|---|---|---|---|
| Main checkout (user) | repo root | 0 | 8000 / 3000 / 5433 / 6380 |
| Agent 2 | `.claude/worktrees/agent-2-security` | 21 | 20420 / 20421 / 20423 / 20424 |
| Agent 3 | `.claude/worktrees/agent-3-data` | 31 | 20620 / 20621 / 20623 / 20624 |
| Lead verification | `.claude/worktrees/lead-verify` | 85 | 21700 / 21701 / 21703 / 21704 |
| Lead trial | `.claude/worktrees/lead-trial` | 86 | from `scripts/stack-env.sh` |
| Lead docs | `.claude/worktrees/lead-docs` | 87 | from `scripts/stack-env.sh` |
| RC assessor (fresh clone) | scratchpad clone | 88 | from `scripts/stack-env.sh` |

**Use `make`, or load `scripts/stack-env.sh`, for every compose command.** The Compose
project name (`COMPOSE_PROJECT_NAME`) comes only from `scripts/stack-env.sh`, which the
Makefile loads. A bare `docker compose ...`, or `scripts/fullstack-lock.sh` run with only
`QUANTLAB_SLOT` set, falls back to the project `quantlab`: that is the **main checkout's**
stack, ports and named volumes. Use `QUANTLAB_SLOT=n make up|app-up|down`, or
`eval "$(scripts/stack-env.sh | sed 's/^/export /')"` first, and check
`echo $COMPOSE_PROJECT_NAME` before any `down`.

**Incident (2026-10-31): E2E data written into the main checkout's volumes.** Two of Agent
2's targeted E2E runs (for #144 and #151) used a wrapper that set `QUANTLAB_SLOT=21` but
didn't load `scripts/stack-env.sh`. So they ran as project `quantlab`, on ports
3000/8000 and the main checkout's named volumes `quantlab_pgdata`, `quantlab_appdata`
and `quantlab_redisdata` (created 2026-10-30).
* **What was written:** E2E test accounts (`e2e-*`, `errors-*`, `ml-panel-*`), synthetic
  datasets, one experiment and one ML run.
* **Schema:** Alembic ran `upgrade head` from branches based on main, so there is no
  schema beyond main.
* **Nothing else touched:** no main-checkout containers were running at either time, so
  `down` stopped nothing of anyone's. The results of those runs stand, and the full
  `make e2e-serial` trials ran on lead slots.
* **Fix:** Agent 2's wrapper now loads stack-env and refuses any other project name.
* **User decision (2026-10-31):** leave all of this data and every volume **untouched**:
  no reset, no row deletion, no file removal. A precise cleanup may be evaluated later,
  after the affected records are identified and a verified backup exists. It needs the
  user's separate explicit approval.

Export `QUANTLAB_SLOT` in every shell (`export QUANTLAB_SLOT=21`), then use `make up`,
`make test`, `make app-up`, `make e2e` as usual. Integration tests use a per-worktree test
database.

**Restart policy on slots (#162).** Compose services restart unless stopped, but
`scripts/stack-env.sh` sets `QUANTLAB_RESTART_POLICY=no` for every slot except the main
checkout's 0. A Docker Desktop restart therefore doesn't bring back every worktree's stack
at once in the shared VM. Named volumes are kept either way. Two consequences:
* A stack left up on a slot stays down after a Docker restart.
* A crash-recovery probe on a worktree slot must set `QUANTLAB_RESTART_POLICY=unless-stopped`
  itself, and still run under `scripts/fullstack-lock.sh run ...`.

**Docker memory budget.** The Docker VM on this machine has 3.8 GiB. A full `make app-up`
stack peaks around 1.7 GB, and three full stacks at once caused API OOM kills
(2026-10-30). Rules:
* For pytest, run `make up` (Postgres + Redis only, under 100 MB).
* Full-stack runs are **serialized**. Use `make e2e-serial`: it takes the lock in
  `scripts/fullstack-lock.sh`, which every worktree of this repository shares through
  git's common directory. A separate clone has its own lock, so stop its stacks first.
  It runs `app-up` with `QUANTLAB_API_WORKERS=2`, Playwright with one worker
  (`E2E_WORKERS=n` to override), then `down`. `scripts/fullstack-lock.sh status` shows
  the holder. A manual `make app-up`, a memory sweep, or a probe that starts an app stack
  must be wrapped in `scripts/fullstack-lock.sh run ...` too. The host has 8 GB of RAM,
  and on 2026-10-31 two concurrent stacks took it to critically low memory.
* Playwright defaults to one worker. With 4 parallel spec files, each registration's
  Argon2id hash (64 MiB × 4 lanes, about 170 ms uncontended) queued behind the others,
  and registrations took 25 s.
* An E2E failure is a regression only if the API wasn't OOM-killed
  (`docker inspect <api> --format '{{.State.OOMKilled}}'`), it reproduces with the failing
  spec run alone, and the report includes `uptime` load. Playwright runs in Actions on
  relevant PRs (#159), on a clean runner. Green CI is evidence there, but it doesn't
  replace the local serial run when a local failure might be a regression.
* Integration tests **fail** when Postgres is unreachable. `make e2e-serial` and `make down`
  stop the project's Postgres too, so run `make up` again before pytest.
  `QUANTLAB_ALLOW_SKIP_INTEGRATION=1` skips them explicitly (unit-only runs).

## Branches, commits and PRs

* Branch from current `main` as `a2/<issue>-<slug>` or `a3/<issue>-<slug>`. One issue per PR,
  with `Closes #N` in the body. The base is always `main` unless the lead says otherwise.
* Keep up to date with `git merge origin/main`. **Never rebase a pushed branch or
  force-push.**
* Conventional commits under the configured Git identity. **No AI attribution trailers,
  signatures or footers** in commits, PRs, issues or docs.
* Never commit secrets, credentials or `var/` data. Never invent credentials.
* Open the PR as a draft while working. Mark it ready only when the checklist below is
  done, then set the issue to `status:in-review`.

### Ready-for-review checklist (the lead verifies each item before merging)
1. Base is `main` and dependencies are merged. Only the intended files changed, and no
   file is owned by the other agent.
2. `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
   pass locally (paste the summary lines), with integration tests run against your own slot.
3. When web changed: `npm run lint && npm run typecheck && npm run build`, plus the
   relevant Playwright specs.
4. Migrations: upgrade, downgrade and upgrade again, all pass on a fresh database.
5. Financial changes: a regression test that fails on `main`, no look-ahead (data dated
   after the decision close is never visible), undefined statistics returned as `None` +
   reason, and synthetic data labelled.
6. Security changes: a negative test for every new trust boundary.
7. CI green on the PR.

The lead reviews (with independent read-only reviewers for P0 work), merges with a merge
commit, and never merges failing or unreviewed changes.

## Escalation
* Blocked on a credential, licence, paid resource or product decision: label the issue
  `status:blocked`, say exactly what is needed, and move on to unblocked work. Only the
  user can supply credentials or authorize spend.
* Need a file you don't own: comment on the issue and wait for the lease. Don't edit it in
  the meantime.
