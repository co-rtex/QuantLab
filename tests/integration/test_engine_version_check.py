"""A backtest is computed only by the engine version it was submitted for (#152, as #143
for ML). Synthetic data only."""

from __future__ import annotations

import uuid

from tests.integration.conftest import run_pending_jobs


def _submit(client, seeded, key: str | None = None) -> dict:
    headers = {**seeded["headers"], **({"Idempotency-Key": key} if key else {})}
    r = client.post(
        f"/api/v1/workspaces/{seeded['ws']}/experiments",
        headers=headers,
        json={
            "name": "engine version",
            "dataset_version_id": seeded["dv"],
            "strategy_key": "equal_weight",
            "strategy_params": {},
            "config": {"start": "2021-01-04", "rebalance": "monthly"},
        },
    )
    assert r.status_code == 202, r.text
    return r.json()


def _job(client, seeded, exp_id: str) -> dict:
    return client.get(f"/api/v1/experiments/{exp_id}/job", headers=seeded["headers"]).json()


def test_a_worker_on_another_engine_refuses_the_run_permanently(client, seeded, monkeypatch):
    from quantlab.backtest.engine import ENGINE_VERSION
    from quantlab_api import tasks

    sub = _submit(client, seeded)
    exp_id = sub["experiment"]["id"]
    assert sub["experiment"]["engine_version"] == ENGINE_VERSION  # stamped at submission
    monkeypatch.setattr(tasks, "ENGINE_VERSION", "99.0.0")  # the worker runs newer code
    assert run_pending_jobs() == ["failed"]
    job = _job(client, seeded, exp_id)
    assert job["error"]["retryable"] is False
    assert (
        f"submitted for engine version {ENGINE_VERSION}, but this worker runs version 99.0.0"
        in job["error"]["message"]
    )
    analysis = client.get(f"/api/v1/experiments/{exp_id}/analysis", headers=seeded["headers"])
    assert analysis.status_code != 200  # no result stored under the old hash


def test_an_unstamped_experiment_still_runs(client, seeded, monkeypatch):
    from quantlab_api import tasks
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Experiment

    exp_id = _submit(client, seeded)["experiment"]["id"]
    with SessionLocal() as db:  # no version recorded (a row written without the stamp)
        db.get(Experiment, uuid.UUID(exp_id)).engine_version = ""
        db.commit()
    monkeypatch.setattr(tasks, "ENGINE_VERSION", "99.0.0")
    assert run_pending_jobs() == ["completed"]


def test_idempotent_replay_is_unaffected(client, seeded, monkeypatch):
    from quantlab_api import tasks

    first = _submit(client, seeded, key="engine-152")
    again = _submit(client, seeded, key="engine-152")  # replayed before the run
    assert again["created"] is False and again["job"]["id"] == first["job"]["id"]
    monkeypatch.setattr(tasks, "ENGINE_VERSION", "99.0.0")
    assert run_pending_jobs() == ["failed"]
    # Replaying the key returns the refused job, not a new run...
    replay = _submit(client, seeded, key="engine-152")
    assert replay["created"] is False and replay["job"]["id"] == first["job"]["id"]
    assert run_pending_jobs() == []
    # ...and submitting again (a new key) once the worker matches computes it.
    monkeypatch.undo()
    fresh = _submit(client, seeded, key="engine-152-again")
    assert fresh["created"] is True and fresh["experiment"]["id"] != first["experiment"]["id"]
    assert fresh["experiment"]["input_hash"] == first["experiment"]["input_hash"]
    assert run_pending_jobs() == ["completed"]
