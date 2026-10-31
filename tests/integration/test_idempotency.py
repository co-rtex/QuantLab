"""Idempotent retries of run-creating requests (#6).

Experiments, ML runs and validation runs insert their run row before ``submit_job``, and
the job payload names that row. Two concurrent requests with one Idempotency-Key used to
both pass the "seen this key?" check, both insert a row, and the loser hashed a payload
with a different row id: 409 ``idempotency_conflict`` for a genuine retry, plus a
phantom run row pointing at nothing.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest
from sqlalchemy import func, select

from tests.integration.conftest import run_pending_jobs
from tests.integration.test_api_workflow import BT
from tests.integration.test_concurrency import _race

N = 6
ML_CONFIG = {"folds": 2, "min_train_dates": 6, "sample_every": 10, "models": ["majority"]}


def _count(model: Any) -> int:
    from quantlab_api.db import SessionLocal

    with SessionLocal() as db:
        return int(db.scalar(select(func.count()).select_from(model)) or 0)


def _experiment(seeded: dict[str, Any], name: str = "Idem") -> tuple[str, dict]:
    return f"/api/v1/workspaces/{seeded['ws']}/experiments", {
        "name": name,
        "dataset_version_id": seeded["dv"],
        "strategy_key": "momentum",
        "strategy_params": {"lookback": 126, "skip": 21, "top_n": 4},
        "config": BT,
    }


def _ml(seeded: dict[str, Any], name: str = "Idem") -> tuple[str, dict]:
    return f"/api/v1/workspaces/{seeded['ws']}/ml-runs", {
        "name": name,
        "dataset_version_id": seeded["dv"],
        "config": ML_CONFIG if name == "Idem" else {**ML_CONFIG, "folds": 3},
    }


def _ids(route: str, body: dict[str, Any]) -> tuple[str, str]:
    """(run id, job id) from a 202 response of either route."""
    if route == "experiment":
        return body["experiment"]["id"], body["job"]["id"]
    return body["id"], body["job"]["id"]


@pytest.mark.parametrize("route", ["experiment", "ml"])
def test_concurrent_retries_create_one_run_and_one_job(client, seeded, route):
    from quantlab_api.models import Experiment, Job, MLRun

    model = Experiment if route == "experiment" else MLRun
    url, body = (_experiment if route == "experiment" else _ml)(seeded)
    headers = {**seeded["headers"], "Idempotency-Key": f"race-{route}"}
    runs_before, jobs_before = _count(model), _count(Job)
    rs = _race(N, lambda c, i: c.post(url, headers=headers, json=body))
    assert Counter(r.status_code for r in rs) == {202: N}, [r.text for r in rs]
    assert len({_ids(route, r.json()) for r in rs}) == 1
    assert _count(model) == runs_before + 1
    assert _count(Job) == jobs_before + 1


def test_concurrent_validation_retries_create_one_run_and_one_job(client, seeded):
    from quantlab_api.models import Job, ResearchRun

    h = seeded["headers"]
    url, body = _experiment(seeded)
    exp = client.post(url, headers=h, json=body).json()["experiment"]["id"]
    assert run_pending_jobs() == ["completed"]
    runs_before, jobs_before = _count(ResearchRun), _count(Job)
    headers = {**h, "Idempotency-Key": "race-validation"}
    rs = _race(N, lambda c, i: c.post(f"/api/v1/experiments/{exp}/validation", headers=headers))
    assert Counter(r.status_code for r in rs) == {202: N}, [r.text for r in rs]
    assert len({(r.json()["id"], r.json()["job"]["id"]) for r in rs}) == 1
    assert _count(ResearchRun) == runs_before + 1
    assert _count(Job) == jobs_before + 1


@pytest.mark.parametrize("route", ["experiment", "ml"])
def test_sequential_replay_creates_no_phantom_row(client, seeded, route):
    from quantlab_api.models import Experiment, Job, MLRun

    model = Experiment if route == "experiment" else MLRun
    url, body = (_experiment if route == "experiment" else _ml)(seeded)
    headers = {**seeded["headers"], "Idempotency-Key": f"seq-{route}"}
    first = client.post(url, headers=headers, json=body)
    assert first.status_code == 202, first.text
    runs, jobs = _count(model), _count(Job)
    again = client.post(url, headers=headers, json=body)
    assert again.status_code == 202, again.text
    assert _ids(route, again.json()) == _ids(route, first.json())
    assert (_count(model), _count(Job)) == (runs, jobs)


@pytest.mark.parametrize("route", ["experiment", "ml"])
def test_different_body_with_the_same_key_is_a_conflict(client, seeded, route):
    from quantlab_api.models import Experiment, Job, MLRun

    model = Experiment if route == "experiment" else MLRun
    build = _experiment if route == "experiment" else _ml
    url, body = build(seeded)
    _, other = build(seeded, name="Different")
    if route == "experiment":
        other["strategy_params"] = {"lookback": 63, "skip": 21, "top_n": 4}
    headers = {**seeded["headers"], "Idempotency-Key": f"conflict-{route}"}
    assert client.post(url, headers=headers, json=body).status_code == 202
    runs, jobs = _count(model), _count(Job)
    clash = client.post(url, headers=headers, json=other)
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "idempotency_conflict"
    assert (_count(model), _count(Job)) == (runs, jobs)


def test_concurrent_conflicting_bodies_admit_one_and_reject_the_rest(client, seeded):
    """Mixed race: the first body wins; the different one is a clean 409, not a 500."""
    from quantlab_api.models import Experiment

    url, body = _experiment(seeded)
    _, other = _experiment(seeded, name="Different")
    other["strategy_params"] = {"lookback": 63, "skip": 21, "top_n": 4}
    headers = {**seeded["headers"], "Idempotency-Key": "mixed"}
    before = _count(Experiment)
    rs = _race(N, lambda c, i: c.post(url, headers=headers, json=body if i % 2 else other))
    codes = Counter(r.status_code for r in rs)
    assert set(codes) <= {202, 409} and codes[202] >= 1, [r.text for r in rs]
    winners = {r.json()["experiment"]["id"] for r in rs if r.status_code == 202}
    assert len(winners) == 1
    assert _count(Experiment) == before + 1


def test_replay_is_not_blocked_by_the_quota_its_own_job_uses(client, seeded, monkeypatch):
    """#2's rule still holds: the original job may be the one filling the cap."""
    from quantlab_api.config import get_settings

    monkeypatch.setattr(get_settings(), "max_active_jobs_per_user", 1)
    url, body = _experiment(seeded)
    headers = {**seeded["headers"], "Idempotency-Key": "quota-replay"}
    first = client.post(url, headers=headers, json=body)
    assert first.status_code == 202, first.text
    rs = _race(N, lambda c, i: c.post(url, headers=headers, json=body))
    assert Counter(r.status_code for r in rs) == {202: N}, [r.text for r in rs]
    other = client.post(url, headers={**seeded["headers"], "Idempotency-Key": "new"}, json=body)
    assert other.status_code == 429  # a genuinely new job is still capped


def _completed_experiment(client, seeded) -> str:  # type: ignore[no-untyped-def]
    url, body = _experiment(seeded)
    exp = client.post(url, headers=seeded["headers"], json=body).json()["experiment"]["id"]
    assert run_pending_jobs() == ["completed"]
    return str(exp)


@pytest.mark.parametrize("keys", ["none", "distinct"])
def test_concurrent_validation_without_a_shared_key_runs_once(client, seeded, keys):
    """Validation is deduplicated per experiment: requests without a key, or with
    different keys, used to race to a unique-constraint 500."""
    from quantlab_api.models import Job, ResearchRun

    exp = _completed_experiment(client, seeded)
    runs, jobs = _count(ResearchRun), _count(Job)

    def call(c, i):  # type: ignore[no-untyped-def]
        h = dict(seeded["headers"])
        if keys == "distinct":
            h["Idempotency-Key"] = f"val-{i}"
        return c.post(f"/api/v1/experiments/{exp}/validation", headers=h)

    rs = _race(N, call)
    assert Counter(r.status_code for r in rs) == {202: N}, [r.text for r in rs]
    assert len({r.json()["id"] for r in rs}) == 1
    assert (_count(ResearchRun), _count(Job)) == (runs + 1, jobs + 1)


def test_failed_validation_replays_with_its_key_and_reruns_once_with_a_new_one(
    client, seeded, monkeypatch
):
    from quantlab_api.models import Job, ResearchRun

    exp = _completed_experiment(client, seeded)
    h = seeded["headers"]
    url = f"/api/v1/experiments/{exp}/validation"
    from quantlab_api import jobs

    def broken(*a: Any, **k: Any) -> Any:
        raise ValueError("deterministic failure")

    handler = jobs.HANDLERS["research.validation"]
    monkeypatch.setitem(jobs.HANDLERS, "research.validation", broken)
    first = client.post(url, headers={**h, "Idempotency-Key": "val-a"})
    assert first.status_code == 202
    run_pending_jobs()
    assert client.get(url, headers=h).json()["status"] == "failed"
    monkeypatch.setitem(jobs.HANDLERS, "research.validation", handler)
    jobs = _count(Job)
    replay = client.post(url, headers={**h, "Idempotency-Key": "val-a"})
    assert replay.status_code == 202 and replay.json()["status"] == "failed"
    assert _count(Job) == jobs  # the same request replayed: nothing reruns
    rs = _race(N, lambda c, i: c.post(url, headers={**h, "Idempotency-Key": "val-b"}))
    assert Counter(r.status_code for r in rs) == {202: N}
    assert _count(Job) == jobs + 1  # one rerun
    assert _count(ResearchRun) == 1


def test_a_key_from_another_request_is_a_conflict_for_validation(client, seeded):
    exp = _completed_experiment(client, seeded)
    h = seeded["headers"]
    url, body = _ml(seeded)
    assert (
        client.post(url, headers={**h, "Idempotency-Key": "shared"}, json=body).status_code == 202
    )
    from quantlab_api.models import ResearchRun

    runs = _count(ResearchRun)
    r = client.post(
        f"/api/v1/experiments/{exp}/validation", headers={**h, "Idempotency-Key": "shared"}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"
    assert _count(ResearchRun) == runs  # no phantom run
