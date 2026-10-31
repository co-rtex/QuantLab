"""Idempotent synthetic-dataset requests (#52): the #43 rules on the dataset route.

A key replays only the same request (same user, same body including name and
description). The key is locked and looked up before the Dataset row is inserted, so
concurrent retries for a new slug no longer race on the slug's unique index.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import func, select

from tests.integration.conftest import join_workspace, register, workspace_id
from tests.integration.test_concurrency import _race

BODY = {"slug": "syn", "name": "S", "n_assets": 2, "start": "2020-01-02", "end": "2020-03-01"}
N = 6


def _count(model: Any) -> int:
    from quantlab_api.db import SessionLocal

    with SessionLocal() as db:
        return int(db.scalar(select(func.count()).select_from(model)) or 0)


def _url(ws: str) -> str:
    return f"/api/v1/workspaces/{ws}/datasets/synthetic"


def test_concurrent_retries_for_a_new_slug_create_one_dataset_and_one_job(client):
    from quantlab_api.models import Dataset, Job

    h = register(client)
    ws = workspace_id(client, h)
    headers = {**h, "Idempotency-Key": "new-slug"}
    rs = _race(N, lambda c, i: c.post(_url(ws), headers=headers, json=BODY))
    assert Counter(r.status_code for r in rs) == {202: N}, [r.text for r in rs]
    assert len({r.json()["id"] for r in rs}) == 1
    assert (_count(Dataset), _count(Job)) == (1, 1)


def test_an_identical_retry_replays(client):
    from quantlab_api.models import Job

    h = register(client)
    ws = workspace_id(client, h)
    headers = {**h, "Idempotency-Key": "same"}
    first = client.post(_url(ws), headers=headers, json=BODY)
    again = client.post(_url(ws), headers=headers, json=BODY)
    assert (first.status_code, again.status_code) == (202, 202)
    assert again.json()["id"] == first.json()["id"]
    assert _count(Job) == 1


def test_a_different_name_or_description_is_a_conflict(client):
    from quantlab_api.models import Job

    h = register(client)
    ws = workspace_id(client, h)
    headers = {**h, "Idempotency-Key": "named"}
    assert client.post(_url(ws), headers=headers, json=BODY).status_code == 202
    for change in ({"name": "Other"}, {"description": "Something else"}):
        r = client.post(_url(ws), headers=headers, json={**BODY, **change})
        assert r.status_code == 409, change
        assert r.json()["error"]["code"] == "idempotency_conflict"
    assert _count(Job) == 1


def test_another_members_key_is_a_conflict_not_their_job(client):
    from quantlab_api.models import Job

    owner = register(client, "owner@example.com")
    ws = workspace_id(client, owner)
    colleague = register(client, "colleague@example.com")
    join_workspace(client, owner, ws, colleague, "colleague@example.com", "editor")
    assert (
        client.post(_url(ws), headers={**owner, "Idempotency-Key": "k"}, json=BODY).status_code
        == 202
    )
    r = client.post(_url(ws), headers={**colleague, "Idempotency-Key": "k"}, json=BODY)
    assert r.status_code == 409
    assert _count(Job) == 1


def test_jobs_from_before_fingerprints_replay_only_for_the_same_user(client):
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Job

    owner = register(client, "owner@example.com")
    ws = workspace_id(client, owner)
    colleague = register(client, "colleague@example.com")
    join_workspace(client, owner, ws, colleague, "colleague@example.com", "editor")
    first = client.post(_url(ws), headers={**owner, "Idempotency-Key": "old"}, json=BODY)
    assert first.status_code == 202
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(Job.idempotency_key == "old"))
        assert job is not None and "request_fingerprint" in job.payload
        job.payload = {k: v for k, v in job.payload.items() if k != "request_fingerprint"}
        db.commit()
    again = client.post(_url(ws), headers={**owner, "Idempotency-Key": "old"}, json=BODY)
    assert again.status_code == 202 and again.json()["id"] == first.json()["id"]
    other = client.post(_url(ws), headers={**colleague, "Idempotency-Key": "old"}, json=BODY)
    assert other.status_code == 409
    changed = {**BODY, "seed": 99}
    assert (
        client.post(_url(ws), headers={**owner, "Idempotency-Key": "old"}, json=changed).status_code
        == 409
    )
