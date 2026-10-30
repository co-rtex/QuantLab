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
