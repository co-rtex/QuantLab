"""Reproduction across an API and worker restart, through a running app stack (#182).

Synthetic data only. Opt-in: it needs a running app stack and restarts its containers, so
it runs only when ``QUANTLAB_STACK_API`` names the stack's API, under the full-stack lock::

    scripts/fullstack-lock.sh run sh -c '
        set -a && eval "$(QUANTLAB_SLOT=31 scripts/stack-env.sh)" && set +a &&
        make app-up &&
        QUANTLAB_STACK_API=http://localhost:$QUANTLAB_API_PORT uv run pytest tests/stack -q;
        make down'

The restart passes ``-p $COMPOSE_PROJECT_NAME``; without a slot's project the test fails
rather than restart the main checkout's stack.

It submits a backtest, reproduces and validates it, restarts the ``api`` and ``worker``
services, then reproduces and validates again: every run must carry the original
fingerprint, and the validation results must be equal.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

API = os.environ.get("QUANTLAB_STACK_API")
ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not API, reason="needs a running app stack: set QUANTLAB_STACK_API (see the docstring)"
)


def _wait(fetch, done, what: str, timeout: float = 600) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fetch()
        if done(value):
            return value
        time.sleep(1)
    raise AssertionError(f"timed out waiting for {what}: {value}")


MAIN_PROJECT = "quantlab"  # the main checkout's compose project: its stack and volumes


def compose_project() -> str:
    """The compose project this test may restart: a worktree slot's, set by
    ``scripts/stack-env.sh``. Never the main checkout's, which a bare ``docker compose``
    in a worktree targets when ``COMPOSE_PROJECT_NAME`` is not exported."""
    name = os.environ.get("COMPOSE_PROJECT_NAME", "").strip()
    if not name or name == MAIN_PROJECT:
        raise RuntimeError(
            f"COMPOSE_PROJECT_NAME is {name or 'unset'!r}: refusing to restart the main "
            f"checkout's stack ({MAIN_PROJECT!r}). Run under "
            '`eval "$(QUANTLAB_SLOT=<n> scripts/stack-env.sh)"` (see the docstring).'
        )
    return name


def _restart() -> None:
    cmd = ["docker", "compose", "-p", compose_project(), "restart", "api", "worker"]
    subprocess.run(cmd, cwd=ROOT, check=True)


def _comparable(result: dict[str, Any]) -> str:
    # The suite's numbers, without fields that name the run rather than describe it.
    skip = {"experiment_id", "research_run_id", "created_at", "finished_at", "duration_s"}
    return json.dumps({k: v for k, v in result.items() if k not in skip}, sort_keys=True)


def test_a_reproduction_after_restarting_the_api_and_worker_matches():
    import httpx

    compose_project()  # fail, not skip, before touching anything, if this is not a slot

    with httpx.Client(base_url=str(API), timeout=60) as c:
        email = f"repro-{uuid.uuid4().hex[:10]}@example.com"
        r = c.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "Restart-repro-2026", "display_name": "repro"},
        )
        assert r.status_code == 201, r.text
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        ws = c.get("/api/v1/workspaces", headers=h).json()[0]["id"]

        def job_done(job_id: str) -> dict[str, Any]:
            job = _wait(
                lambda: c.get(f"/api/v1/jobs/{job_id}", headers=h).json(),
                lambda j: j["status"] in ("completed", "failed", "cancelled"),
                f"job {job_id}",
            )
            assert job["status"] == "completed", job
            return job

        r = c.post(
            f"/api/v1/workspaces/{ws}/datasets/synthetic",
            headers=h,
            json={
                "slug": "repro",
                "name": "Synthetic",
                "n_assets": 10,
                "seed": 3,
                "start": "2019-01-02",
                "end": "2021-12-31",
            },
        )
        assert r.status_code == 202, r.text
        job_done(r.json()["id"])
        dv = c.get(f"/api/v1/workspaces/{ws}/dataset-versions", headers=h).json()[0]["id"]

        def experiment(exp_id: str) -> dict[str, Any]:
            return _wait(
                lambda: c.get(f"/api/v1/experiments/{exp_id}", headers=h).json(),
                lambda e: e["status"] in ("completed", "failed", "cancelled"),
                f"experiment {exp_id}",
            )

        def validate(exp_id: str) -> str:
            r = c.post(f"/api/v1/experiments/{exp_id}/validation", headers=h, json={})
            assert r.status_code == 202, r.text
            run = _wait(
                lambda: c.get(f"/api/v1/experiments/{exp_id}/validation", headers=h).json(),
                lambda v: v["status"] in ("completed", "failed"),
                f"validation of {exp_id}",
            )
            assert run["status"] == "completed", run
            return _comparable(run["result"])

        def reproduce(exp_id: str) -> dict[str, Any]:
            r = c.post(f"/api/v1/experiments/{exp_id}/reproduce", headers=h)
            assert r.status_code == 202, r.text
            child = experiment(r.json()["experiment"]["id"])
            assert child["status"] == "completed", child
            check = c.get(f"/api/v1/experiments/{child['id']}/reproducibility", headers=h).json()
            assert [x["fingerprint_match"] for x in check["comparisons"]] == [True], check
            return child

        r = c.post(
            f"/api/v1/workspaces/{ws}/experiments",
            headers=h,
            json={
                "name": "restart",
                "dataset_version_id": dv,
                "strategy_key": "momentum",
                "strategy_params": {"lookback": 63, "skip": 5, "top_n": 4},
                "config": {"start": "2019-07-01", "rebalance": "weekly", "benchmark": "SYNIDX"},
            },
        )
        assert r.status_code == 202, r.text
        original = experiment(r.json()["experiment"]["id"])
        assert original["status"] == "completed", original
        before = reproduce(original["id"])
        assert before["fingerprint"] == original["fingerprint"]
        validated = validate(before["id"])

        _restart()
        _wait(lambda: _health(c), bool, "the API after the restart", timeout=180)

        after = reproduce(original["id"])
        assert after["fingerprint"] == original["fingerprint"]
        assert after["input_hash"] == original["input_hash"]
        assert validate(after["id"]) == validated


def _health(c: Any) -> bool:
    import httpx

    try:
        return c.get("/api/v1/health").status_code == 200
    except httpx.HTTPError:  # the API is restarting
        return False
