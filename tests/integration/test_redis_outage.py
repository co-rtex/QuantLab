"""A busy worker outlasts a Redis outage, and the job loses no attempt (#185).

Before, RQ's horse monitor heartbeat raised ConnectionError and the worker quit: in a
container its running horse died with it, the job lost an attempt, and the container
crash-looped until Redis returned. Here a real forking worker (redis_outage_child.py) runs
two queued jobs, with an outage from the middle of the first until after it ends.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from sqlalchemy import select

from quantlab_api import jobs as J
from quantlab_api.db import SessionLocal
from quantlab_api.models import Job, JobStatus, User, Workspace

ROOT = Path(__file__).resolve().parents[2]


def _job(payload: dict) -> uuid.UUID:
    with SessionLocal() as db:
        user, ws = db.scalar(select(User)), db.scalar(select(Workspace))
        job, _ = J.submit_job(
            db,
            workspace_id=ws.id,
            user_id=user.id,
            kind="outage.probe",
            payload=payload,
            dispatch=False,
        )
        return job.id


def _status(jid: uuid.UUID) -> tuple[str, int]:
    with SessionLocal() as db:
        job = db.get(Job, jid)
        return job.status.value, job.attempts


def _run(
    kind: str, tmp_path: Path
) -> tuple[subprocess.CompletedProcess[str], uuid.UUID, uuid.UUID]:
    flag = tmp_path / "redis-down"
    first, second = _job({"outage": True}), _job({})
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.integration.redis_outage_child",
            str(flag),
            kind,
            str(first),
            str(second),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # The outage starts inside job 1; end it a few seconds after job 1 is recorded done,
    # so RQ's bookkeeping after the horse also met a dead Redis.
    deadline = time.monotonic() + 60
    while not flag.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert flag.exists(), "the outage never started"
    while _status(first)[0] not in ("completed", "failed") and time.monotonic() < deadline:
        time.sleep(0.2)
    time.sleep(3)
    flag.unlink()
    out, _ = proc.communicate(timeout=90)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, ""), first, second


def test_a_busy_worker_outlasts_a_redis_outage_and_takes_the_next_job(seeded, tmp_path):
    done, first, second = _run("quantlab", tmp_path)
    assert done.returncode == 0, done.stdout[-3000:]
    assert _status(first) == ("completed", 1)  # no attempt lost
    assert _status(second) == ("completed", 1)  # the same worker went on
    assert "found an unhandled exception, quitting" not in done.stdout
    assert "worker_redis_unavailable" in done.stdout  # it did meet the outage


def test_rq_s_own_worker_quits_at_the_first_failed_heartbeat(seeded, tmp_path):
    """The control: the defect itself."""
    done, _, second = _run("plain", tmp_path)
    assert "found an unhandled exception, quitting" in done.stdout, done.stdout[-3000:]
    assert _status(second) == (JobStatus.queued.value, 0)  # nobody took the next job
