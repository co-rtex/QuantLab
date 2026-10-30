"""Concurrent requests against a real PostgreSQL database (threads, one session each)."""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable
from typing import Any

from tests.integration.conftest import register, workspace_id


def _race(n: int, call: Callable[[Any, int], Any]) -> list[Any]:
    """Run ``call(client, i)`` in ``n`` threads released at the same instant."""
    from fastapi.testclient import TestClient

    from quantlab_api.main import app

    out: list[Any] = [None] * n
    barrier = threading.Barrier(n)

    def run(i: int) -> None:
        with TestClient(app, raise_server_exceptions=False) as c:
            barrier.wait()
            out[i] = call(c, i)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


SYNTH = {"name": "S", "n_assets": 2, "start": "2020-01-02", "end": "2020-03-01"}


def test_concurrent_job_submissions_in_one_workspace_do_not_deadlock(client):
    """Each submission inserts rows referencing the workspace (FK KEY SHARE locks) before
    submit_job locked it FOR UPDATE, so concurrent submissions deadlocked: 10-11 of 12
    returned 500."""
    h = register(client)
    ws = workspace_id(client, h)
    codes = _race(
        12,
        lambda c, i: (
            c.post(
                f"/api/v1/workspaces/{ws}/datasets/synthetic",
                headers=h,
                json={**SYNTH, "slug": f"race-{i}"},
            ).status_code
        ),
    )
    assert Counter(codes) == {202: 12}


def test_concurrent_retries_with_one_idempotency_key_return_the_same_job(client):
    h = register(client)
    ws = workspace_id(client, h)
    client.post(
        f"/api/v1/workspaces/{ws}/datasets/synthetic", headers=h, json={**SYNTH, "slug": "idem"}
    )
    rs = _race(
        8,
        lambda c, i: c.post(
            f"/api/v1/workspaces/{ws}/datasets/synthetic",
            headers={**h, "Idempotency-Key": "retry-1"},
            json={**SYNTH, "slug": "idem"},
        ),
    )
    assert Counter(r.status_code for r in rs) == {202: 8}, [r.text for r in rs]
    assert len({r.json()["id"] for r in rs}) == 1


def test_workspace_with_completed_backtest_and_validation_can_be_deleted(client, seeded):
    """experiment_results / research_runs / ml_runs referenced jobs without ON DELETE, so
    deleting the workspace failed with a foreign-key 409 that no retry could resolve."""
    from tests.integration.conftest import run_pending_jobs
    from tests.integration.test_api_workflow import submit

    h = seeded["headers"]
    exp_id = submit(client, seeded).json()["experiment"]["id"]
    assert run_pending_jobs() == ["completed"]
    assert client.post(f"/api/v1/experiments/{exp_id}/validation", headers=h).status_code == 202
    run_pending_jobs()
    r = client.delete(f"/api/v1/workspaces/{seeded['ws']}", headers=h)
    assert r.status_code == 204, r.text
    assert client.get(f"/api/v1/experiments/{exp_id}", headers=h).status_code == 404


def _owner_count(ws: str) -> int:
    from sqlalchemy import func, select

    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Role, WorkspaceMember

    with SessionLocal() as db:
        return db.scalar(
            select(func.count())
            .select_from(WorkspaceMember)
            .where(WorkspaceMember.workspace_id == ws, WorkspaceMember.role == Role.owner)
        )


def test_two_owners_demoting_or_removing_each_other_leave_one_owner(client):
    """The last-owner check counted owners without a lock, so concurrent requests both saw
    two owners and the workspace ended with none (9-10 of 10 trials in the audit)."""
    for action in ("demote", "remove"):
        for trial in range(5):
            tag = f"{action}{trial}"
            a = register(client, f"a-{tag}@example.com")
            b = register(client, f"b-{tag}@example.com")
            ws = workspace_id(client, a)
            uid = {
                "a": client.get("/api/v1/auth/me", headers=a).json()["id"],
                "b": client.get("/api/v1/auth/me", headers=b).json()["id"],
            }
            r = client.post(
                f"/api/v1/workspaces/{ws}/members",
                headers=a,
                json={"email": f"b-{tag}@example.com", "role": "owner"},
            )
            assert r.status_code == 201, r.text
            # Each owner targets the other one.
            plan = [(a, "b"), (b, "a")]

            def act(c, i, plan=plan, uid=uid, ws=ws, tag=tag, action=action):  # type: ignore[no-untyped-def]
                h, other = plan[i]
                if action == "remove":
                    return c.delete(
                        f"/api/v1/workspaces/{ws}/members/{uid[other]}", headers=h
                    ).status_code
                return c.post(
                    f"/api/v1/workspaces/{ws}/members",
                    headers=h,
                    json={"email": f"{other}-{tag}@example.com", "role": "viewer"},
                ).status_code

            codes = sorted(_race(2, act))
            assert codes not in ([201, 201], [204, 204]), (action, codes)
            assert _owner_count(ws) == 1, (action, codes)



def test_user_job_quota_spans_workspaces_and_replays_do_not_consume_it(client, monkeypatch):
    """One account cannot multiply its job allowance by creating workspaces."""
    from quantlab_api.config import get_settings

    h = register(client)
    ws1 = workspace_id(client, h)
    created = client.post("/api/v1/workspaces", headers=h, json={"name": "Second"})
    assert created.status_code == 201, created.text
    ws2 = created.json()["id"]
    monkeypatch.setattr(get_settings(), "max_active_jobs_per_user", 2)

    first = client.post(
        f"/api/v1/workspaces/{ws1}/datasets/synthetic",
        headers={**h, "Idempotency-Key": "user-quota-replay"},
        json={**SYNTH, "slug": "user-cap-a"},
    )
    second = client.post(
        f"/api/v1/workspaces/{ws2}/datasets/synthetic",
        headers=h,
        json={**SYNTH, "slug": "user-cap-b"},
    )
    assert first.status_code == second.status_code == 202
    replay = client.post(
        f"/api/v1/workspaces/{ws1}/datasets/synthetic",
        headers={**h, "Idempotency-Key": "user-quota-replay"},
        json={**SYNTH, "slug": "user-cap-a"},
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == first.json()["id"]
    full = client.post(
        f"/api/v1/workspaces/{ws2}/datasets/synthetic",
        headers=h,
        json={**SYNTH, "slug": "user-cap-c"},
    )
    assert full.status_code == 429, full.text
    assert full.json()["error"]["code"] == "too_many_jobs"

    # Cancellation and other accounts should not remain blocked by the user's quota.
    assert (
        client.post(f"/api/v1/jobs/{first.json()['id']}/cancel", headers=h).status_code
        == 200
    )
    resumed = client.post(
        f"/api/v1/workspaces/{ws2}/datasets/synthetic",
        headers=h,
        json={**SYNTH, "slug": "user-cap-c"},
    )
    assert resumed.status_code == 202, resumed.text
    other = register(client, "quota-other@example.com")
    other_ws = workspace_id(client, other)
    assert (
        client.post(
            f"/api/v1/workspaces/{other_ws}/datasets/synthetic",
            headers=other,
            json={**SYNTH, "slug": "user-cap-other"},
        ).status_code
        == 202
    )


def test_concurrent_user_quota_is_atomic_across_workspaces(client, monkeypatch):
    """Two parallel requests to different workspaces must share one account budget."""
    from quantlab_api.config import get_settings

    h = register(client)
    ws1 = workspace_id(client, h)
    created = client.post("/api/v1/workspaces", headers=h, json={"name": "Second"})
    assert created.status_code == 201
    ws2 = created.json()["id"]
    monkeypatch.setattr(get_settings(), "max_active_jobs_per_user", 1)

    responses = _race(
        2,
        lambda c, i: c.post(
            f"/api/v1/workspaces/{(ws1, ws2)[i]}/datasets/synthetic",
            headers=h,
            json={**SYNTH, "slug": f"account-race-{i}"},
        ),
    )
    assert Counter(r.status_code for r in responses) == {202: 1, 429: 1}, [
        r.text for r in responses
    ]


def test_concurrent_workspace_creation_enforces_per_user_quota(client, monkeypatch):
    from quantlab_api.config import get_settings

    h = register(client)
    monkeypatch.setattr(get_settings(), "max_workspaces_per_user", 2)
    responses = _race(
        2,
        lambda c, i: c.post(
            "/api/v1/workspaces", headers=h, json={"name": f"Extra {i}"}
        ),
    )
    assert Counter(r.status_code for r in responses) == {201: 1, 429: 1}, [
        r.text for r in responses
    ]
    assert len(client.get("/api/v1/workspaces", headers=h).json()) == 2



def test_huge_offsets_are_rejected_not_500(client):
    h = register(client)
    ws = workspace_id(client, h)
    for path in (f"/api/v1/workspaces/{ws}/jobs", f"/api/v1/workspaces/{ws}/experiments"):
        assert client.get(f"{path}?offset={10**19}", headers=h).status_code == 422


def test_metrics_method_label_cannot_be_chosen_by_clients(client):
    for i in range(5):
        client.request(f"X{i:04d}", "/api/v1/nope")
    body = client.get("/metrics").text
    assert 'method="X0001"' not in body
    assert 'method="OTHER"' in body
