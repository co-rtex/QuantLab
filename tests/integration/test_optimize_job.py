"""Portfolio optimization as a worker job (#224, part 1).

The job's stored result must be the synchronous route's response for the same request:
that route is removed in part 2, so these comparisons are the record of equivalence.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

from quantlab_api.config import get_settings
from quantlab_api.db import SessionLocal
from quantlab_api.models import Job, OptimizationRun
from tests.integration.conftest import join_workspace, register, run_pending_jobs

SYMS = [f"SYN{i:03d}" for i in range(1, 9)]
BODIES: list[dict[str, Any]] = [
    {"symbols": SYMS, "as_of": "2021-12-31", "max_weight": 0.3, "benchmark": "SYNIDX"},
    {"symbols": SYMS, "as_of": "2021-06-30"},
    {"symbols": SYMS[:3], "lookback": 60, "covariance": "sample", "methods": ["risk_parity"]},
    {"symbols": [s.lower() for s in SYMS[2:6]], "risk_aversion": 20, "benchmark": "NOPE"},
]


def submit(client, h, dv, body, key=None):  # type: ignore[no-untyped-def]
    headers = {**h, "Idempotency-Key": key} if key else h
    return client.post(f"/api/v1/dataset-versions/{dv}/optimizations", headers=headers, json=body)


def jobs_count() -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Job).where(Job.kind == "optimize")) or 0


@pytest.mark.parametrize("body", BODIES)
def test_the_job_result_equals_the_synchronous_response(client, seeded, body) -> None:
    h, dv = seeded["headers"], seeded["dv"]
    sync = client.post(f"/api/v1/dataset-versions/{dv}/optimize", headers=h, json=body)
    assert sync.status_code == 200, sync.text
    r = submit(client, h, dv, body)
    assert r.status_code == 202, r.text
    queued = r.json()
    assert queued["status"] == "queued" and queued["result"] is None
    assert queued["job"]["kind"] == "optimize"
    assert run_pending_jobs() == ["completed"]
    got = client.get(f"/api/v1/optimizations/{queued['id']}", headers=h)
    assert got.status_code == 200, got.text
    run = got.json()
    assert run["status"] == "completed" and run["job"]["status"] == "completed"
    assert run["result"] == sync.json()
    assert run["params"]["symbols"] == body["symbols"]


def test_a_viewer_reads_but_cannot_submit(client, seeded) -> None:
    h, dv, ws = seeded["headers"], seeded["dv"], seeded["ws"]
    viewer = register(client, "viewer@example.com")
    join_workspace(client, h, ws, viewer, "viewer@example.com", role="viewer")
    r = submit(client, viewer, dv, BODIES[0])
    assert r.status_code == 403, r.text
    assert jobs_count() == 0
    run = submit(client, h, dv, BODIES[0]).json()["id"]
    assert client.get(f"/api/v1/optimizations/{run}", headers=viewer).status_code == 200


def test_a_non_member_can_neither_submit_nor_read(client, seeded) -> None:
    h, dv = seeded["headers"], seeded["dv"]
    run = submit(client, h, dv, BODIES[0]).json()["id"]
    outsider = register(client, "outsider@example.com")
    assert submit(client, outsider, dv, BODIES[0]).status_code == 404
    r = client.get(f"/api/v1/optimizations/{run}", headers=outsider)
    assert r.status_code == 404 and r.json()["error"]["message"] == "Optimization not found"


def test_symbols_the_version_does_not_list_are_refused_before_a_job(client, seeded) -> None:
    h, dv = seeded["headers"], seeded["dv"]
    r = submit(client, h, dv, {"symbols": ["SYN001", "ZZZ9", "QQQ9"]})
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "unknown_symbols" and err["details"] == ["QQQ9", "ZZZ9"]
    assert jobs_count() == 0


@pytest.mark.parametrize(
    ("body", "message"),
    [
        # Listed, but no bar on or before as_of: the job finds out.
        ({"symbols": SYMS[:3], "as_of": "2019-12-31"}, "Symbols without data up to as_of"),
        (
            {"symbols": SYMS[:3], "as_of": "2020-03-31", "lookback": 252},
            "Fewer than two symbols have enough history in the look-back window",
        ),
    ],
)
def test_data_problems_fail_the_job_with_the_routes_message(client, seeded, body, message) -> None:
    h, dv = seeded["headers"], seeded["dv"]
    sync = client.post(f"/api/v1/dataset-versions/{dv}/optimize", headers=h, json=body)
    assert sync.status_code == 422 and sync.json()["error"]["message"] == message
    run = submit(client, h, dv, body).json()["id"]
    assert run_pending_jobs() == ["failed"]  # permanent: no retry
    out = client.get(f"/api/v1/optimizations/{run}", headers=h).json()
    assert out["status"] == "failed" and out["result"] is None
    assert out["job"]["error"]["message"].startswith(message)
    assert out["job"]["error"]["retryable"] is False


def test_an_idempotent_retry_returns_the_same_run(client, seeded) -> None:
    h, dv = seeded["headers"], seeded["dv"]
    first = submit(client, h, dv, BODIES[0], key="opt-1").json()
    again = submit(client, h, dv, BODIES[0], key="opt-1")
    assert again.status_code == 202 and again.json()["id"] == first["id"]
    assert jobs_count() == 1
    other = submit(client, h, dv, BODIES[1], key="opt-1")
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_conflict"


def test_submissions_share_the_per_minute_budget(client, seeded, monkeypatch) -> None:
    h, dv = seeded["headers"], seeded["dv"]
    monkeypatch.setattr(get_settings(), "rate_limit_optimize_per_minute", 2)
    assert submit(client, h, dv, BODIES[0]).status_code == 202
    assert submit(client, h, dv, BODIES[1]).status_code == 202
    r = submit(client, h, dv, BODIES[2])
    assert r.status_code == 429 and r.headers["Retry-After"]
    assert jobs_count() == 2


def test_a_job_cap_refusal_does_not_spend_the_budget(client, seeded, monkeypatch) -> None:
    h, dv = seeded["headers"], seeded["dv"]
    monkeypatch.setattr(get_settings(), "rate_limit_optimize_per_minute", 2)
    monkeypatch.setattr(get_settings(), "max_active_jobs_per_user", 1)
    assert submit(client, h, dv, BODIES[0]).status_code == 202
    for _ in range(3):  # refused by the cap, so none of them is one of the 2
        r = submit(client, h, dv, BODIES[1])
        assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_jobs"
    with SessionLocal() as db:  # the refused requests left no run behind
        assert db.scalar(select(func.count()).select_from(OptimizationRun)) == 1
    assert run_pending_jobs() == ["completed"]
    assert submit(client, h, dv, BODIES[1]).status_code == 202  # the second of the 2


def test_cancelling_a_queued_optimization(client, seeded) -> None:
    h, dv = seeded["headers"], seeded["dv"]
    run = submit(client, h, dv, BODIES[0]).json()
    r = client.post(f"/api/v1/jobs/{run['job']['id']}/cancel", headers=h)
    assert r.status_code == 200, r.text
    assert run_pending_jobs() == []
    out = client.get(f"/api/v1/optimizations/{run['id']}", headers=h).json()
    assert out["status"] == "cancelled" and out["result"] is None


def test_deleting_the_workspace_deletes_its_optimizations(client, seeded) -> None:
    h, dv, ws = seeded["headers"], seeded["dv"], seeded["ws"]
    run = submit(client, h, dv, BODIES[0]).json()["id"]
    assert run_pending_jobs() == ["completed"]
    assert client.delete(f"/api/v1/workspaces/{ws}", headers=h).status_code == 204
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(OptimizationRun)) == 0
    assert client.get(f"/api/v1/optimizations/{run}", headers=h).status_code == 404
