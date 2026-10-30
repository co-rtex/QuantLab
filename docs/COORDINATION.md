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
| `services/api/src/quantlab_api/security.py`, `limits.py`, `routes/auth.py` | Agent 2 |
| `services/api/src/quantlab_api/routes/workspaces.py` (members, invitations) | Agent 2 |
| `services/api/src/quantlab_api/routes/datasets.py` | **Agent 2 until #4 and #7 merge**, then Agent 3 |
| `apps/web/src/app/api/[...path]/route.ts`, `apps/web/src/proxy.ts`, `infra/edge/**` | Agent 2 |
| Web pages for login, settings, members, invitations | Agent 2 |
| `routes/experiments.py`, `routes/ml.py`, `routes/research.py` (idempotency only, #6) | Agent 2 while #6 is open |
| `packages/quantlab/src/quantlab/data/**`, `services/api/src/quantlab_api/marketdata.py` | Agent 3 |
| `packages/quantlab/src/quantlab/{backtest,analytics,research,ml,portfolio,optimize,strategies}/**` | Agent 3 |
| `docs/METHODOLOGY.md` | Agent 3 |
| `packages/quantlab/src/quantlab/assistant/**`, other web pages, `benchmarks/**` | Unassigned (lead assigns per issue) |

## Shared files (lead-owned, leased one holder at a time)

A worker edits a shared file only while it holds a lease recorded here. Ask for one by
commenting on the issue; the lead updates this table in the same PR that grants it or in a
follow-up docs commit. A lease ends when the PR that used it merges or closes.

| Shared file | Current holder | For |
|---|---|---|
| `services/api/src/quantlab_api/models.py` | — | |
| `services/api/src/quantlab_api/schemas.py` | — | |
| `services/api/src/quantlab_api/config.py` | — | |
| `services/api/src/quantlab_api/jobs.py` | — | |
| `services/api/src/quantlab_api/tasks.py` | — | |
| `services/api/src/quantlab_api/main.py` | — | |
| `services/api/alembic/versions/**` | see migration slots | |
| `docker-compose.yml`, `Makefile`, `scripts/stack-env.sh` | — | |
| `pyproject.toml`, `uv.lock`, `apps/web/package*.json` | — | |
| `.github/workflows/**` | lead only | |
| `DECISIONS.md` | — (Agent 2 may edit the ADR-0008 section under #3) | |
| `STATUS.md`, `ROADMAP.md`, `PROJECT_SPEC.md`, `docs/ARCHITECTURE.md`, this file | lead only | |

Workers report status in their PR description and issue comments, not in `STATUS.md`.

## Migration slots

Alembic revisions form one linear chain, so only one migration may be in flight at a time.

| Slot | Holder | Issue | State |
|---|---|---|---|
| M1 | Agent 2 | #5 invitations | reserved, not started |
| M2 | Agent 3 | #9 instrument identity | queued; may start only after M1 merges or the lead re-sequences |

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

Export `QUANTLAB_SLOT` in every shell (`export QUANTLAB_SLOT=21`), then use `make up`,
`make test`, `make app-up`, `make e2e` as usual. Integration tests use a per-worktree test
database.

**Docker memory budget.** The Docker VM on this machine has 3.8 GiB. A full `make app-up`
stack peaks around 1.7 GB, and three full stacks at once caused API OOM kills
(2026-10-30). Rules:
* For pytest, run `make up` (Postgres + Redis only, under 100 MB).
* Run at most **one** full `app-up` stack per agent, with `QUANTLAB_API_WORKERS=2`, and
  `make down` as soon as E2E finishes.
* An OOM-killed API (`docker inspect <api> --format '{{.State.OOMKilled}}'`) invalidates
  an E2E result. Rerun it; don't report it as a pass or as a code regression.

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
