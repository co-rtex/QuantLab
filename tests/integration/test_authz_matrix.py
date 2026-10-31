"""Route-wide authorization matrix (#133).

Every API route is classified in ``ROUTES``: public, user-scoped (any signed-in user, for
their own things) or workspace-scoped with a minimum role. A new route fails
``test_every_route_is_classified`` until it is added there. Two workspaces, A and B, each
hold one resource of every kind, and for every route the matrix checks that:

- an unauthenticated client gets 401 (public routes aside);
- an outsider (a member of B only) gets 404 with A's ids, and nothing of A's in the body;
- a member of A below the route's minimum role gets 403;
- A's owner (or the named caller) gets the route's success status, so the denials above
  are not a 401 or a 404 everywhere;
- A's workspace in the path with a child id of B's gets 404 (the classic IDOR: checking the
  workspace, then loading the child by id alone), as do B's ids in a request body.

Non-members get 404, never 403, so ids cannot be probed (``require_workspace``); members
without the role get 403. One seeded world is shared by the module, for runtime: the
allowed calls that delete or revoke use resources of their own, and run last.
Synthetic data only.
"""

from __future__ import annotations

import io
import re
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import fakeredis
import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute, iter_route_contexts
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text
from starlette.routing import BaseRoute, Mount, Route

from quantlab_api.models import Role
from tests.integration.conftest import (
    TABLES,
    accept_token,
    join_workspace,
    register,
    run_pending_jobs,
    workspace_id,
)

PUBLIC, USER, WORKSPACE = "public", "user", "workspace"
V, E, O = Role.viewer, Role.editor, Role.owner
PASSWORD = "Correct-horse-9"
OWNER_EMAIL, OUTSIDER_EMAIL = "owner-a@example.com", "owner-b@example.com"
REPORT = "reconciliation-matrix"
BT = {
    "start": "2021-01-04",
    "rebalance": "monthly",
    "benchmark": "SYNIDX",
    "commission_bps": 2,
    "slippage_bps": 5,
}
ML = {"folds": 2, "min_train_dates": 6, "sample_every": 10, "models": ["majority"]}
BARS = "symbol,date,open,high,low,close,volume\nabc,2024-01-02,10,10.5,9.8,10.2,1000\n"
BARS += "abc,2024-01-03,10.2,10.4,10.0,10.1,1200\nabc,2024-01-04,10.1,10.3,9.9,10.0,900\n"


@dataclass
class World:
    client: TestClient
    headers: dict[str, dict[str, str]]  # by caller; "anon" has none
    a: dict[str, str]  # A's value for each path parameter
    b: dict[str, str]  # B's
    spare: dict[str, str]  # resources of A's that the allowed deletes use up, and tokens
    secrets: dict[str, set[str]]  # by side: what no response to the other side may contain


Body = Callable[[World], dict[str, Any]]


@dataclass(frozen=True)
class Spec:
    access: str
    role: Role | None = None  # the minimum role in the workspace, for a workspace route
    ok: int = 200  # the allowed call's status
    body: Body | None = None  # request kwargs: json, files, data
    caller: str = "owner"  # who makes the allowed call
    use: dict[str, str] = field(default_factory=dict)  # path parameter -> World.spare key
    swap: tuple[str, ...] = ()  # path parameters naming a child of the path's workspace
    last: bool = False  # the allowed call deletes or revokes something: run it last


def _fresh(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _token(key: str) -> Body:
    return lambda w: {"json": {"token": w.spare[key]}}


def _experiment(w: World) -> dict[str, Any]:
    return {
        "json": {
            "name": "probe",
            "dataset_version_id": w.a["version_id"],
            "strategy_key": "equal_weight",
            "strategy_params": {},
            "config": BT,
        }
    }


def _csv(w: World) -> dict[str, Any]:
    return {
        "files": {"bars": ("bars.csv", io.BytesIO(BARS.encode()), "text/csv")},
        "data": {
            "slug": _fresh("probe"),
            "name": "probe",
            "source": "Broker export",
            "license": "Personal research use",
        },
    }


W = "/api/v1/workspaces/{workspace_id}"
ROUTES: dict[str, Spec] = {
    # system
    "GET /api/v1/openapi.json": Spec(PUBLIC, caller="anon"),
    "GET /api/v1/docs": Spec(PUBLIC, caller="anon"),
    "GET /docs/oauth2-redirect": Spec(PUBLIC, caller="anon"),
    "GET /api/v1/health": Spec(PUBLIC, caller="anon"),
    "GET /api/v1/ready": Spec(PUBLIC, caller="anon"),
    "GET /api/v1/version": Spec(PUBLIC, caller="anon"),
    "GET /metrics": Spec(PUBLIC, caller="anon"),  # a token is required in production
    # identity
    "POST /api/v1/auth/register": Spec(
        PUBLIC,
        ok=201,
        caller="anon",
        body=lambda w: {
            "json": {
                "email": f"{_fresh('new')}@example.com",
                "password": PASSWORD,
                "display_name": "New",
            }
        },
    ),
    "POST /api/v1/auth/login": Spec(
        PUBLIC,
        caller="anon",
        body=lambda w: {"json": {"email": OWNER_EMAIL, "password": PASSWORD}},
    ),
    "POST /api/v1/auth/logout": Spec(USER, ok=204),
    "GET /api/v1/auth/me": Spec(USER),
    "POST /api/v1/auth/invitations/preview": Spec(USER, caller="carol", body=_token("carol_token")),
    "POST /api/v1/auth/invitations/accept": Spec(USER, caller="dave", body=_token("dave_token")),
    "POST /api/v1/auth/invitations/decline": Spec(
        USER, ok=204, caller="carol", body=_token("carol_token"), last=True
    ),
    # workspaces and members
    "GET /api/v1/workspaces": Spec(USER),
    "POST /api/v1/workspaces": Spec(USER, ok=201, body=lambda w: {"json": {"name": "probe"}}),
    f"GET {W}": Spec(WORKSPACE, V),
    f"PATCH {W}": Spec(WORKSPACE, E, body=lambda w: {"json": {"name": "alpha-workspace"}}),
    f"DELETE {W}": Spec(WORKSPACE, O, ok=204, use={"workspace_id": "workspace_id"}, last=True),
    f"GET {W}/members": Spec(WORKSPACE, V),
    f"PATCH {W}/members/{{user_id}}": Spec(
        WORKSPACE, O, body=lambda w: {"json": {"role": "editor"}}, swap=("user_id",)
    ),
    f"DELETE {W}/members/{{user_id}}": Spec(WORKSPACE, O, ok=204, swap=("user_id",), last=True),
    f"GET {W}/audit": Spec(WORKSPACE, O),
    f"POST {W}/invitations": Spec(
        WORKSPACE,
        O,
        ok=201,
        body=lambda w: {"json": {"email": f"{_fresh('invite')}@example.com", "role": "viewer"}},
    ),
    f"GET {W}/invitations": Spec(WORKSPACE, O),
    f"DELETE {W}/invitations/{{invitation_id}}": Spec(
        WORKSPACE,
        O,
        ok=204,
        use={"invitation_id": "invitation_id"},
        swap=("invitation_id",),
        last=True,
    ),
    # datasets
    f"GET {W}/datasets": Spec(WORKSPACE, V),
    f"POST {W}/datasets/synthetic": Spec(
        WORKSPACE,
        E,
        ok=202,
        body=lambda w: {
            "json": {
                "slug": _fresh("probe"),
                "name": "probe",
                "n_assets": 3,
                "start": "2021-01-04",
                "end": "2021-03-31",
                "seed": 1,
            }
        },
    ),
    f"POST {W}/datasets/csv": Spec(WORKSPACE, E, ok=202, body=_csv),
    "GET /api/v1/datasets/{dataset_id}/versions": Spec(WORKSPACE, V),
    f"GET {W}/dataset-versions": Spec(WORKSPACE, V),
    "GET /api/v1/dataset-versions/{version_id}": Spec(WORKSPACE, V),
    "GET /api/v1/dataset-versions/{version_id}/instruments": Spec(WORKSPACE, V),
    "GET /api/v1/dataset-versions/{version_id}/bars/{symbol}": Spec(WORKSPACE, V),
    "POST /api/v1/dataset-versions/{version_id}/optimize": Spec(
        WORKSPACE,
        V,  # a computation over the version, no write
        body=lambda w: {"json": {"symbols": ["SYN001", "SYN002", "SYN003"], "as_of": "2021-12-31"}},
    ),
    f"GET {W}/dataset-versions/{{version_id}}/reconciliations": Spec(
        WORKSPACE, V, swap=("version_id",)
    ),
    f"GET {W}/dataset-versions/{{version_id}}/reconciliations/{{name}}": Spec(
        WORKSPACE, V, swap=("version_id",)
    ),
    # strategies
    "GET /api/v1/strategies/catalog": Spec(PUBLIC, caller="anon"),
    f"GET {W}/strategies": Spec(WORKSPACE, V),
    f"POST {W}/strategies": Spec(
        WORKSPACE,
        E,
        ok=201,
        body=lambda w: {
            "json": {"name": "probe", "strategy_key": "sma_crossover", "params": {"fast": 5}}
        },
    ),
    f"DELETE {W}/strategies/{{strategy_id}}": Spec(
        WORKSPACE, E, ok=204, use={"strategy_id": "strategy_id"}, swap=("strategy_id",), last=True
    ),
    # experiments and research
    f"POST {W}/experiments": Spec(WORKSPACE, E, ok=202, body=_experiment),
    f"GET {W}/experiments": Spec(WORKSPACE, V),
    f"POST {W}/experiments/compare": Spec(
        WORKSPACE, V, body=lambda w: {"json": {"experiment_ids": [w.a["experiment_id"]]}}
    ),
    "GET /api/v1/experiments/{experiment_id}": Spec(WORKSPACE, V),
    "GET /api/v1/experiments/{experiment_id}/job": Spec(WORKSPACE, V),
    "GET /api/v1/experiments/{experiment_id}/analysis": Spec(WORKSPACE, V),
    "GET /api/v1/experiments/{experiment_id}/equity": Spec(WORKSPACE, V),
    "GET /api/v1/experiments/{experiment_id}/trades": Spec(WORKSPACE, V),
    "GET /api/v1/experiments/{experiment_id}/positions": Spec(WORKSPACE, V),
    "POST /api/v1/experiments/{experiment_id}/reproduce": Spec(WORKSPACE, E, ok=202),
    "GET /api/v1/experiments/{experiment_id}/reproducibility": Spec(WORKSPACE, V),
    "GET /api/v1/experiments/{experiment_id}/manifest": Spec(WORKSPACE, V),
    "POST /api/v1/experiments/{experiment_id}/validation": Spec(WORKSPACE, E, ok=202),
    "GET /api/v1/experiments/{experiment_id}/validation": Spec(WORKSPACE, V),
    "GET /api/v1/experiments/{experiment_id}/integrity": Spec(WORKSPACE, V),
    "GET /api/v1/experiments/{experiment_id}/report": Spec(WORKSPACE, V),
    "POST /api/v1/experiments/{experiment_id}/assistant": Spec(
        WORKSPACE, V, body=lambda w: {"json": {"question": "What was the max drawdown?"}}
    ),
    # jobs (ADR-0011)
    "GET /api/v1/jobs/{job_id}": Spec(WORKSPACE, V),
    "POST /api/v1/jobs/{job_id}/cancel": Spec(WORKSPACE, E),
    f"GET {W}/jobs": Spec(WORKSPACE, V),
    # ML
    f"POST {W}/ml-runs": Spec(
        WORKSPACE,
        E,
        ok=202,
        body=lambda w: {
            "json": {"name": "probe", "dataset_version_id": w.a["version_id"], "config": ML}
        },
    ),
    f"GET {W}/ml-runs": Spec(WORKSPACE, V),
    "GET /api/v1/ml-runs/{run_id}": Spec(WORKSPACE, V),
    "GET /api/v1/ml-runs/{run_id}/predictions": Spec(WORKSPACE, V),
    # portfolios
    f"POST {W}/portfolios": Spec(
        WORKSPACE,
        E,
        ok=201,
        body=lambda w: {
            "json": {
                "name": "probe",
                "dataset_version_id": w.a["version_id"],
                "initial_deposit": 1000,
                "start_date": "2021-01-04",
            }
        },
    ),
    f"GET {W}/portfolios": Spec(WORKSPACE, V),
    "GET /api/v1/portfolios/{portfolio_id}": Spec(WORKSPACE, V),
    "POST /api/v1/portfolios/{portfolio_id}/transactions": Spec(
        WORKSPACE,
        E,
        ok=201,
        body=lambda w: {
            "json": {"trade_date": "2021-01-05", "kind": "buy", "symbol": "SYN001", "quantity": 1}
        },
    ),
    "GET /api/v1/portfolios/{portfolio_id}/valuation": Spec(WORKSPACE, V),
}

# Findings filed on #133, by (check, route): each is a strict xfail until its route is fixed.
FINDINGS: dict[tuple[str, str], str] = {}


DOCS = {"/api/v1/openapi.json", "/api/v1/docs", "/docs/oauth2-redirect"}  # FastAPI's own


def routes_of(routes: Sequence[BaseRoute]) -> set[str]:
    """``METHOD path`` for every HTTP route the app serves, through included routers.

    Anything else (a mount, a websocket route, a plain Starlette route besides FastAPI's
    docs) comes out as ``<Type> path``, which no ``ROUTES`` key matches: it fails the
    classification, because these checks cannot reach it."""
    out = set()
    for ctx in iter_route_contexts(routes):
        route, path = ctx.original_route, getattr(ctx.original_route, "path", "?")
        methods = (ctx.methods or set()) - {"HEAD"}  # HEAD rides along with GET
        http = isinstance(route, APIRoute) or (type(route) is Route and ctx.path in DOCS)
        if http and methods:
            out.update(f"{method} {ctx.path}" for method in methods)
        else:
            out.add(f"{type(route).__name__} {ctx.path or path}")
    return out


def table_problems(table: dict[str, Spec]) -> list[str]:
    """Classifications the checks would silently skip."""
    out = []
    for key, s in table.items():
        params = set(re.findall(r"{(\w+)}", key))
        if s.access in (PUBLIC, USER) and params:
            out.append(f"{key}: a {s.access} route takes {sorted(params)}")
        if s.access == WORKSPACE and (s.role is None or not params):
            out.append(f"{key}: a workspace route needs a role and an id")
        nested = "workspace_id" in params and params - {"workspace_id"}
        if s.access == WORKSPACE and nested and not s.swap:
            out.append(f"{key}: nested ids, but no swap= for the cross-workspace check")
        if not set(s.swap) <= params:
            out.append(f"{key}: swap= names {sorted(set(s.swap) - params)}, not in the path")
    return out


def _params(check: str, keys: list[str]) -> list[Any]:
    out = []
    for key in keys:
        reason = FINDINGS.get((check, key))
        marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
        out.append(pytest.param(key, id=key, marks=marks))
    return out


def _keys(pred: Callable[[Spec], bool]) -> list[str]:
    return [k for k, s in ROUTES.items() if pred(s)]


# --------------------------------------------------------------------------- the world
@pytest.fixture(autouse=True)
def clean() -> Iterator[None]:
    """Overrides the suite's per-test truncate: this module shares one seeded world."""
    yield


def _truncate() -> None:
    from quantlab_api.db import get_engine

    with get_engine().begin() as c:
        c.execute(text("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"))


def _invite(c: TestClient, h: dict[str, str], ws: str, email: str) -> tuple[str, str]:
    r = c.post(f"{W.format(workspace_id=ws)}/invitations", headers=h, json={"email": email})
    assert r.status_code == 201, r.text
    return r.json()["id"], accept_token(r)


def _seed_workspace(
    c: TestClient, h: dict[str, str], ws: str, tag: str, seed: int
) -> dict[str, str]:
    """One resource of every kind in ``ws``, named with ``tag`` (a leak marker). The seeds
    differ, so the two versions' content hashes do too."""
    base = W.format(workspace_id=ws)
    assert c.patch(base, headers=h, json={"name": f"{tag}-workspace"}).status_code == 200
    r = c.post(
        f"{base}/datasets/synthetic",
        headers=h,
        json={
            "slug": f"{tag}-data",
            "name": f"{tag}-data",
            "n_assets": 12,
            "start": "2020-01-02",
            "end": "2022-12-30",
            "seed": seed,
        },
    )
    assert r.status_code == 202, r.text
    assert set(run_pending_jobs()) == {"completed"}  # and any runs still queued before it
    dataset = c.get(f"{base}/datasets", headers=h).json()[0]["id"]
    (version_row,) = c.get(f"{base}/dataset-versions", headers=h).json()
    version = version_row["id"]
    strategy = c.post(
        f"{base}/strategies",
        headers=h,
        json={"name": f"{tag}-strategy", "strategy_key": "sma_crossover", "params": {"fast": 5}},
    ).json()["id"]
    r = c.post(
        f"{base}/experiments",
        headers=h,
        json={
            "name": f"{tag}-experiment",
            "dataset_version_id": version,
            "strategy_key": "equal_weight",
            "strategy_params": {},
            "config": BT,
        },
    )
    assert r.status_code == 202, r.text
    experiment = r.json()["experiment"]["id"]
    r = c.post(
        f"{base}/ml-runs",
        headers=h,
        json={"name": f"{tag}-ml", "dataset_version_id": version, "config": ML},
    )
    assert r.status_code == 202, r.text
    run = r.json()["id"]
    r = c.post(
        f"{base}/portfolios",
        headers=h,
        json={
            "name": f"{tag}-portfolio",
            "dataset_version_id": version,
            "initial_deposit": 1000,
            "start_date": "2021-01-04",
        },
    )
    assert r.status_code == 201, r.text
    job = c.get(f"/api/v1/experiments/{experiment}/job", headers=h).json()["id"]
    invitation, _ = _invite(c, h, ws, f"pending-{tag}@example.com")
    _write_report(version)
    return {
        "workspace_id": ws,
        "dataset_id": dataset,
        "version_id": version,
        "symbol": "SYN001",
        "strategy_id": strategy,
        "experiment_id": experiment,
        "job_id": job,
        "run_id": run,
        "portfolio_id": r.json()["id"],
        "invitation_id": invitation,
        "name": REPORT,
        "version_hash": version_row["version_hash"],  # not a path parameter: a secret
    }


def _write_report(dv_id: str) -> None:
    from quantlab.data.reconcile import Source, reconcile
    from quantlab.data.store import DatasetStore
    from quantlab_api.config import get_settings
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Dataset, DatasetVersion

    with SessionLocal() as db:
        row = db.get(DatasetVersion, uuid.UUID(dv_id))
        assert row is not None
        ds = db.get(Dataset, row.dataset_id)
        assert ds is not None
    store = DatasetStore(get_settings().datasets_dir)
    version = store.open(ds.slug, row.version_hash)
    primary = Source.from_version(store, version, "primary", is_synthetic=True)
    report = reconcile(primary, Source("vendor-x", primary.bars, primary.actions, None, True))
    store.write_report(version, REPORT, report.to_dict(), owner=dv_id)


def _user_id(c: TestClient, h: dict[str, str]) -> str:
    return str(c.get("/api/v1/auth/me", headers=h).json()["id"])


@pytest.fixture(scope="module")
def world(database: None) -> Iterator[World]:
    try:
        with pytest.MonkeyPatch.context() as mp:
            yield from _world(mp)
    finally:
        _truncate()


def _world(mp: pytest.MonkeyPatch) -> Iterator[World]:
    from quantlab.assistant.evals import FaithfulProvider
    from quantlab_api.jobs import set_redis
    from quantlab_api.main import app
    from quantlab_api.routes import research
    from quantlab_api.security import login_limiter

    # The assistant's allowed call answers from a fake model, whatever key the environment
    # holds: this suite never reaches a real provider.
    mp.setattr(research, "_provider", FaithfulProvider)
    _truncate()
    set_redis(fakeredis.FakeRedis())
    login_limiter.reset()
    with TestClient(app) as c:
        h = {
            "owner": register(c, OWNER_EMAIL, PASSWORD),
            "outsider": register(c, OUTSIDER_EMAIL, PASSWORD),  # owns B, nothing in A
            "viewer": register(c, "viewer-a@example.com", PASSWORD),
            "editor": register(c, "editor-a@example.com", PASSWORD),  # and a viewer of B
            "spare": register(c, "spare-a@example.com", PASSWORD),
            "carol": register(c, "carol@example.com", PASSWORD),
            "dave": register(c, "dave@example.com", PASSWORD),
        }
        ws_a, ws_b = workspace_id(c, h["owner"]), workspace_id(c, h["outsider"])
        join_workspace(c, h["owner"], ws_a, h["viewer"], "viewer-a@example.com", "viewer")
        join_workspace(c, h["owner"], ws_a, h["editor"], "editor-a@example.com", "editor")
        join_workspace(c, h["outsider"], ws_b, h["editor"], "editor-a@example.com", "viewer")
        join_workspace(c, h["owner"], ws_a, h["spare"], "spare-a@example.com", "viewer")
        a = _seed_workspace(c, h["owner"], ws_a, "alpha", seed=5)
        b = _seed_workspace(c, h["outsider"], ws_b, "bravo", seed=7)
        assert set(run_pending_jobs()) == {"completed"}  # both workspaces' runs
        r = c.post(f"/api/v1/experiments/{a['experiment_id']}/validation", headers=h["owner"])
        assert r.status_code == 202, r.text
        assert run_pending_jobs() == ["completed"]
        a["user_id"] = _user_id(c, h["spare"])
        b["user_id"] = _user_id(c, h["outsider"])  # a member of B only
        spare_ws = c.post("/api/v1/workspaces", headers=h["owner"], json={"name": "alpha-spare"})
        spare_strategy = c.post(
            f"{W.format(workspace_id=ws_a)}/strategies",
            headers=h["owner"],
            json={"name": "alpha-spare", "strategy_key": "sma_crossover", "params": {"fast": 5}},
        )
        spare_invitation, _ = _invite(c, h["owner"], ws_a, "pending-alpha-2@example.com")
        _, carol = _invite(c, h["owner"], ws_a, "carol@example.com")
        _, dave = _invite(c, h["owner"], ws_a, "dave@example.com")
        spare = {
            "workspace_id": spare_ws.json()["id"],
            "strategy_id": spare_strategy.json()["id"],
            "invitation_id": spare_invitation,
            "carol_token": carol,
            "dave_token": dave,
        }
        people = {k: _user_id(c, v) for k, v in h.items()}
        secrets = {
            "a": {
                "alpha-",
                *a.values(),
                *(v for k, v in spare.items() if not k.endswith("_token")),
                *(people[k] for k in ("owner", "viewer", "editor", "spare", "carol", "dave")),
                OWNER_EMAIL,
                "viewer-a@example.com",
                "editor-a@example.com",
                "spare-a@example.com",
                "carol@example.com",
                "dave@example.com",
                "pending-alpha@example.com",
                "pending-alpha-2@example.com",
            },
            "b": {
                "bravo-",
                *b.values(),
                people["outsider"],
                OUTSIDER_EMAIL,
                "pending-bravo@example.com",
            },
        }
        for side in secrets.values():  # what both sides share, or names a route's literal
            side -= {"SYN001", REPORT}
        c.cookies.clear()
        yield World(client=c, headers={"anon": {}, **h}, a=a, b=b, spare=spare, secrets=secrets)
        # Run what the allowed calls queued (an upload's staged files among them), so the
        # truncate leaves nothing on disk without a row; later modules check for that.
        outcomes = run_pending_jobs(max_jobs=50)
        assert "failed" not in outcomes, outcomes


def call(
    w: World, key: str, caller: str, values: dict[str, str], body: Body | None = None
) -> Response:
    method, path = key.split(" ", 1)
    spec = ROUTES[key]
    kwargs = (body or spec.body or (lambda _: {}))(w)
    w.client.cookies.clear()  # the caller's headers are its only credentials
    r = w.client.request(method, path.format(**values), headers=w.headers[caller], **kwargs)
    w.client.cookies.clear()
    return r


def _leaks(w: World, r: Response, side: str) -> list[str]:
    """``side``'s names, emails, ids and hashes in the response, except those the request
    itself carried (a 404 may echo the id it was asked for)."""
    sent = str(r.request.url) + r.request.read().decode(errors="replace")
    return sorted(s for s in w.secrets[side] if s in r.text and s not in sent)


# --------------------------------------------------------------------------- the matrix
def test_every_route_is_classified() -> None:
    from quantlab_api.main import app

    served = routes_of(app.routes)
    assert sorted(served - set(ROUTES)) == [], "classify the new route(s) in ROUTES"
    assert sorted(set(ROUTES) - served) == [], "ROUTES names a route the app no longer has"


def test_an_unclassified_route_is_caught() -> None:
    """The check above fails on a route nobody classified (here, dummy ones), including
    kinds these checks cannot exercise: a mount and a websocket route."""
    from quantlab_api.main import app

    def dummy() -> dict[str, str]:
        return {}

    async def socket() -> None:
        return None

    extra = [
        APIRoute("/api/v1/workspaces/{workspace_id}/unclassified", dummy, methods=["GET"]),
        Mount("/x", routes=[]),
        APIWebSocketRoute("/api/v1/ws/{workspace_id}", socket),
        Route("/plain", dummy, methods=["GET"]),
    ]
    served = routes_of([*app.routes, *extra])
    assert served - set(ROUTES) == {
        "GET /api/v1/workspaces/{workspace_id}/unclassified",
        "Mount /x",
        "APIWebSocketRoute /api/v1/ws/{workspace_id}",
        "Route /plain",
    }


def test_the_table_is_consistent() -> None:
    """A classification the checks would skip fails here: ids on a public or user route, or
    a nested workspace route without the cross-workspace swap."""
    assert table_problems(ROUTES) == []
    nested = f"GET {W}/things/{{thing_id}}"
    assert table_problems(
        {nested: Spec(WORKSPACE, V), "GET /api/v1/things/{thing_id}": Spec(USER)}
    ) == [
        f"{nested}: nested ids, but no swap= for the cross-workspace check",
        "GET /api/v1/things/{thing_id}: a user route takes ['thing_id']",
    ]


def test_every_body_id_is_swapped() -> None:
    """Each id a request body names is tried with B's value (``SWAPS``): a new one is not
    covered until it is added there."""
    from quantlab_api.main import app

    schema, named = app.openapi(), set()
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            for content in op.get("requestBody", {}).get("content", {}).values():
                ref = content["schema"].get("$ref", "")
                fields = schema["components"]["schemas"].get(ref.split("/")[-1], content["schema"])
                for f in fields.get("properties", {}):
                    if f.endswith(("_id", "_ids")):
                        named.add((f"{method.upper()} {path}", f))
    assert named == {(key, f) for key, f, _ in SWAPS.values()}


@pytest.mark.parametrize("key", _params("anon", _keys(lambda s: s.access != PUBLIC)))
def test_unauthenticated_is_refused(world: World, key: str) -> None:
    assert call(world, key, "anon", world.a).status_code == 401


@pytest.mark.parametrize("key", _params("outsider", _keys(lambda s: s.access == WORKSPACE)))
def test_an_outsider_gets_404_and_nothing_of_a(world: World, key: str) -> None:
    r = call(world, key, "outsider", world.a)
    assert r.status_code == 404, r.text
    assert _leaks(world, r, "a") == [], r.text


def test_an_outsider_is_not_shown_a(world: World) -> None:
    listed = world.client.get("/api/v1/workspaces", headers=world.headers["outsider"]).json()
    assert world.a["workspace_id"] not in {w["id"] for w in listed}
    listed = world.client.get("/api/v1/workspaces", headers=world.headers["viewer"]).json()
    assert world.a["workspace_id"] in {w["id"] for w in listed}  # the check can see it


ROLED = _keys(lambda s: s.access == WORKSPACE and s.role in (E, O))


@pytest.mark.parametrize("key", _params("role", ROLED))
def test_a_member_below_the_role_gets_403(world: World, key: str) -> None:
    for caller in ["viewer", "editor"] if ROUTES[key].role == O else ["viewer"]:
        r = call(world, key, caller, world.a)
        assert r.status_code == 403, (caller, r.text)


@pytest.mark.parametrize("key", _params("role-in-b", ROLED))
def test_a_role_in_a_does_not_carry_to_b(world: World, key: str) -> None:
    """The editor of A is a viewer of B: with B's ids, every editor or owner route refuses
    them, whether the workspace is in the path or only behind the resource's id."""
    r = call(world, key, "editor", world.b)
    assert r.status_code == 403, r.text


@pytest.mark.parametrize(
    "key", _params("swap", _keys(lambda s: s.access == WORKSPACE and bool(s.swap)))
)
def test_a_child_of_b_under_a_is_not_found(world: World, key: str) -> None:
    """A's workspace in the path, a child of B's beside it: the owner of A (no member of B)
    must get 404, not B's resource."""
    values = {**world.a, **{p: world.b[p] for p in ROUTES[key].swap}}
    r = call(world, key, "owner", values)
    assert r.status_code == 404, r.text
    assert _leaks(world, r, "b") == [], r.text


# Each id a request body names, with B's value (route, field, body).
SWAPS: dict[str, tuple[str, str, Body]] = {
    "experiment on B's version": (
        f"POST {W}/experiments",
        "dataset_version_id",
        lambda w: {"json": {**_experiment(w)["json"], "dataset_version_id": w.b["version_id"]}},
    ),
    "experiment with B's strategy": (
        f"POST {W}/experiments",
        "strategy_id",
        lambda w: {
            "json": {
                **_experiment(w)["json"],
                "strategy_key": None,
                "strategy_id": w.b["strategy_id"],
            }
        },
    ),
    "ML run on B's version": (
        f"POST {W}/ml-runs",
        "dataset_version_id",
        lambda w: {
            "json": {"name": "probe", "dataset_version_id": w.b["version_id"], "config": ML}
        },
    ),
    "portfolio on B's version": (
        f"POST {W}/portfolios",
        "dataset_version_id",
        lambda w: {
            "json": {
                "name": "probe",
                "dataset_version_id": w.b["version_id"],
                "initial_deposit": 1000,
                "start_date": "2021-01-04",
            }
        },
    ),
    "compare with B's experiment": (
        f"POST {W}/experiments/compare",
        "experiment_ids",
        lambda w: {"json": {"experiment_ids": [w.b["experiment_id"]]}},
    ),
}


@pytest.mark.parametrize(
    ("caller", "expected"),
    [("owner", 404), ("editor", 422)],
    ids=["a-member-of-a-only", "a-member-of-both"],
)
@pytest.mark.parametrize("case", list(SWAPS))
def test_a_body_naming_a_resource_of_b_is_refused(
    world: World, case: str, caller: str, expected: int
) -> None:
    """B's ids in the body of a request to A. To a member of A only, B's resources do not
    exist (404); a member of both is told the resource belongs to another workspace (422),
    except where the route reports any foreign child as not found."""
    key, _, body = SWAPS[case]
    if caller == "editor" and case in (
        "experiment with B's strategy",
        "compare with B's experiment",
    ):
        expected = 404  # these routes look the child up within A only
    r = call(world, key, caller, world.a, body)
    assert r.status_code == expected, r.text
    if expected == 422:  # the route's refusal, not a schema error from a mistyped field
        assert "another workspace" in r.json()["error"]["message"], r.text
    assert _leaks(world, r, "b") == [], r.text


def test_a_job_of_b_cannot_be_read_or_cancelled_through_a(world: World) -> None:
    """ADR-0011: a job is reached by its id alone, and authorized by its own workspace."""
    c, h, job = world.client, world.headers, world.b["job_id"]
    for caller in ("owner", "viewer"):  # members of A only
        assert c.get(f"/api/v1/jobs/{job}", headers=h[caller]).status_code == 404
        assert c.post(f"/api/v1/jobs/{job}/cancel", headers=h[caller]).status_code == 404
    # The editor of A is a viewer of B: B's role governs B's job, not A's.
    assert c.get(f"/api/v1/jobs/{job}", headers=h["editor"]).status_code == 200
    assert c.post(f"/api/v1/jobs/{job}/cancel", headers=h["editor"]).status_code == 403
    listed = c.get(f"{W.format(workspace_id=world.a['workspace_id'])}/jobs", headers=h["owner"])
    assert job not in {j["id"] for j in listed.json()["items"]}
    status = c.get(f"/api/v1/jobs/{job}", headers=h["outsider"]).json()["status"]
    assert status in ("queued", "completed")  # never cancelled by the attempts above


@pytest.mark.parametrize(
    "key",
    _params(
        "allowed",
        # Deletes and revokes last: these cases rely on running in this order.
        _keys(lambda s: not s.last) + _keys(lambda s: s.last),
    ),
)
def test_the_allowed_caller_gets_through(world: World, key: str) -> None:
    """Not vacuous: each route answers its allowed caller, so the 401, 403 and 404 above
    come from authorization, not from a broken request."""
    spec = ROUTES[key]
    values = {**world.a, **{p: world.spare[k] for p, k in spec.use.items()}}
    r = call(world, key, spec.caller, values)
    assert r.status_code == spec.ok, r.text
    if key.endswith("/assistant"):
        assert r.json()["provider"] == "fake:faithful"  # never a real model
