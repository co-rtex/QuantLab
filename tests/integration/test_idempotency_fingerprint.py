"""What an Idempotency-Key replays (#43).

A key replays only the same request: the same endpoint (and path ids), the same user and
the same body, name and notes included. Keys are scoped to the workspace. The earlier
fingerprint covered only a run's inputs, so a key reused for a reproduction, another
parent or another member silently returned someone else's run.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest
from sqlalchemy import func, select, update

from tests.integration.conftest import join_workspace, register
from tests.integration.test_concurrency import _race
from tests.integration.test_idempotency import N, _completed_experiment, _count, _experiment, _ml


def _key(seeded: dict[str, Any], key: str) -> dict[str, str]:
    return {**seeded["headers"], "Idempotency-Key": key}


def test_a_key_used_to_create_is_a_conflict_for_reproduce(client, seeded):
    from quantlab_api.models import Experiment

    url, body = _experiment(seeded)
    first = client.post(url, headers=_key(seeded, "K"), json=body)
    assert first.status_code == 202, first.text
    exp = first.json()["experiment"]["id"]
    before = _count(Experiment)
    r = client.post(f"/api/v1/experiments/{exp}/reproduce", headers=_key(seeded, "K"))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"
    assert _count(Experiment) == before


def test_reproducing_another_parent_with_the_same_key_is_a_conflict(client, seeded):
    url, body = _experiment(seeded)
    x = client.post(url, headers=seeded["headers"], json=body).json()["experiment"]["id"]
    y = client.post(url, headers=seeded["headers"], json=body).json()["experiment"]["id"]
    first = client.post(f"/api/v1/experiments/{x}/reproduce", headers=_key(seeded, "R"))
    assert first.status_code == 202, first.text
    again = client.post(f"/api/v1/experiments/{x}/reproduce", headers=_key(seeded, "R"))
    assert again.json()["experiment"]["id"] == first.json()["experiment"]["id"]
    other = client.post(f"/api/v1/experiments/{y}/reproduce", headers=_key(seeded, "R"))
    assert other.status_code == 409


def test_concurrent_reproduce_retries_create_one_run(client, seeded):
    from quantlab_api.models import Experiment, Job

    url, body = _experiment(seeded)
    x = client.post(url, headers=seeded["headers"], json=body).json()["experiment"]["id"]
    runs, jobs = _count(Experiment), _count(Job)
    rs = _race(
        N,
        lambda c, i: c.post(f"/api/v1/experiments/{x}/reproduce", headers=_key(seeded, "RR")),
    )
    assert Counter(r.status_code for r in rs) == {202: N}, [r.text for r in rs]
    assert len({r.json()["experiment"]["id"] for r in rs}) == 1
    assert (_count(Experiment), _count(Job)) == (runs + 1, jobs + 1)


@pytest.mark.parametrize("field", ["name", "notes"])
def test_a_different_name_or_note_is_a_different_request(client, seeded, field):
    url, body = _experiment(seeded)
    assert client.post(url, headers=_key(seeded, f"f-{field}"), json=body).status_code == 202
    changed = {**body, field: "Something else"}
    r = client.post(url, headers=_key(seeded, f"f-{field}"), json=changed)
    assert r.status_code == 409


def test_a_key_another_member_used_is_a_conflict(client, seeded):
    """Keys are scoped to the workspace; replaying another member's run would hand them
    a run they did not ask for."""
    from quantlab_api.models import Experiment

    colleague = register(client, "colleague@example.com")
    join_workspace(
        client, seeded["headers"], seeded["ws"], colleague, "colleague@example.com", "editor"
    )
    url, body = _experiment(seeded)
    assert client.post(url, headers=_key(seeded, "shared"), json=body).status_code == 202
    before = _count(Experiment)
    r = client.post(url, headers={**colleague, "Idempotency-Key": "shared"}, json=body)
    assert r.status_code == 409
    assert _count(Experiment) == before


def test_validation_with_a_foreign_key_is_a_conflict_even_when_a_run_exists(client, seeded):
    exp = _completed_experiment(client, seeded)
    h = seeded["headers"]
    url = f"/api/v1/experiments/{exp}/validation"
    assert client.post(url, headers=h).status_code == 202  # a run exists, queued
    ml_url, ml_body = _ml(seeded)
    assert client.post(ml_url, headers=_key(seeded, "ml-key"), json=ml_body).status_code == 202
    r = client.post(url, headers=_key(seeded, "ml-key"))
    assert r.status_code == 409  # used to be a 202 with the validation run
    assert client.post(url, headers=_key(seeded, "val-key")).status_code == 202
    assert client.post(url, headers=_key(seeded, "val-key")).status_code == 202  # replay


def test_an_empty_key_is_rejected_not_a_500(client, seeded):
    exp = _completed_experiment(client, seeded)
    url, body = _experiment(seeded)
    ml_url, ml_body = _ml(seeded)
    calls = [
        ("post", url, body),
        ("post", ml_url, ml_body),
        ("post", f"/api/v1/experiments/{exp}/validation", None),
        ("post", f"/api/v1/experiments/{exp}/reproduce", None),
        (
            "post",
            f"/api/v1/workspaces/{seeded['ws']}/datasets/synthetic",
            {"slug": "e", "name": "E", "n_assets": 2, "start": "2020-01-02", "end": "2020-03-01"},
        ),
    ]
    for _ in range(2):  # the second empty key used to hit the unique constraint: 500
        for method, path, payload in calls:
            r = client.request(method, path, headers=_key(seeded, ""), json=payload)
            assert r.status_code == 422, (path, r.status_code, r.text)


def test_jobs_from_before_fingerprints_still_replay_identical_retries(client, seeded):
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Experiment, Job

    url, body = _experiment(seeded)
    first = client.post(url, headers=_key(seeded, "legacy"), json=body)
    assert first.status_code == 202
    with SessionLocal() as db:  # make it look like a job created before #43
        job = db.scalar(select(Job).where(Job.idempotency_key == "legacy"))
        assert job is not None and "request_fingerprint" in job.payload
        job.payload = {k: v for k, v in job.payload.items() if k != "request_fingerprint"}
        db.commit()
    before = _count(Experiment)
    again = client.post(url, headers=_key(seeded, "legacy"), json=body)
    assert again.status_code == 202
    assert again.json()["experiment"]["id"] == first.json()["experiment"]["id"]
    other = {**body, "strategy_params": {"lookback": 63, "skip": 21, "top_n": 4}}
    assert client.post(url, headers=_key(seeded, "legacy"), json=other).status_code == 409
    assert _count(Experiment) == before


def test_an_unlinked_ml_job_from_before_40_replays_instead_of_409_forever(client, seeded):
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Job, MLRun

    url, body = _ml(seeded)
    first = client.post(url, headers=_key(seeded, "unlinked"), json=body)
    assert first.status_code == 202
    with SessionLocal() as db:  # the link commit never happened, and no fingerprint
        db.execute(update(MLRun).values(job_id=None))
        job = db.scalar(select(Job).where(Job.idempotency_key == "unlinked"))
        assert job is not None
        job.payload = {k: v for k, v in job.payload.items() if k != "request_fingerprint"}
        db.commit()
    again = client.post(url, headers=_key(seeded, "unlinked"), json=body)
    assert again.status_code == 202, again.text
    assert again.json()["id"] == first.json()["id"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(MLRun)) == 1


def test_a_waiter_gives_up_instead_of_holding_a_connection(client, seeded, monkeypatch):
    """A request waiting on the same key's lock times out with 409, not forever."""
    import uuid

    from sqlalchemy import text

    from quantlab_api import jobs
    from quantlab_api.db import SessionLocal

    monkeypatch.setattr(jobs, "IDEMPOTENCY_WAIT", "300ms")
    name = f"idempotency:{uuid.UUID(seeded['ws'])}:held"
    holder = SessionLocal()
    try:
        holder.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": name})
        url, body = _experiment(seeded)
        r = client.post(url, headers=_key(seeded, "held"), json=body)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "request_in_progress"
    finally:
        holder.rollback()
        holder.close()
    url, body = _experiment(seeded)
    assert client.post(url, headers=_key(seeded, "held"), json=body).status_code == 202
