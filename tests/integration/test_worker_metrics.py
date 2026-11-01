"""Job metrics reach a worker's scraped endpoint (#165).

They are recorded in RQ's forked work horses, so each test runs a real worker in a fresh
interpreter (worker_metrics_child.py) with its own multiprocess directory, as a worker
container has, and reads what its /metrics endpoint serves.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import select

from quantlab_api import jobs as J
from quantlab_api.db import SessionLocal
from quantlab_api.models import Job, JobStatus, User, Workspace

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "m" * 24


def _job(payload: dict) -> uuid.UUID:
    with SessionLocal() as db:
        user, ws = db.scalar(select(User)), db.scalar(select(Workspace))
        job, _ = J.submit_job(
            db,
            workspace_id=ws.id,
            user_id=user.id,
            kind="metrics.probe",
            payload=payload,
            dispatch=False,
        )
        return job.id


def _worker(
    directory: Path, kind: str, job: uuid.UUID | None = None
) -> subprocess.CompletedProcess[str]:
    directory.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(directory), "PYTHONPATH": str(ROOT)}
    # Port 0: the child's server takes a free port itself (#194). Picking one here and
    # binding it in the child raced other processes for it ("Address already in use").
    args = ["0", TOKEN, kind, *([str(job)] if job else [])]
    return subprocess.run(
        [sys.executable, "-m", "tests.integration.worker_metrics_child", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _value(text: str, name: str, **labels: str) -> float | None:
    """The sample ``name{labels}``, or None when it is not served."""
    for line in text.splitlines():
        m = re.fullmatch(r"([a-zA-Z_:][\w:]*)(?:\{(.*)\})? (\S+)", line)
        if not m or m.group(1) != name:
            continue
        got = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2) or ""))
        if all(got.get(k) == v for k, v in labels.items()):
            return float(m.group(3))
    return None


def _running(text: str) -> float:
    return _value(text, "quantlab_jobs_running") or 0.0


def test_a_completed_job_is_counted_at_the_workers_scraped_endpoint(seeded, tmp_path):
    job_id = _job({})
    out = _worker(tmp_path, "quantlab", job_id)
    assert out.returncode == 0, out.stderr[-2000:]
    m = out.stdout
    assert _value(m, "quantlab_job_outcomes_total", kind="metrics.probe", outcome="completed") == 1
    assert _value(m, "quantlab_job_run_seconds_count", kind="metrics.probe") == 1
    assert _value(m, "quantlab_job_queue_wait_seconds_count", kind="metrics.probe") == 1
    assert _running(m) == 0
    with SessionLocal() as db:
        assert db.get(Job, job_id).status == JobStatus.completed


def test_a_horse_killed_mid_job_does_not_leave_jobs_running_raised(seeded, tmp_path):
    out = _worker(tmp_path / "ours", "quantlab", _job({"die": "horse"}))
    assert out.returncode == 0, out.stderr[-2000:]
    assert _running(out.stdout) == 0
    # The control: rq's own worker leaves the dead horse's gauge at 1.
    control = _worker(tmp_path / "plain", "plain", _job({"die": "horse"}))
    assert control.returncode == 0, control.stderr[-2000:]
    assert _running(control.stdout) == 1


def test_a_container_killed_mid_job_restarts_with_jobs_running_at_0(seeded, tmp_path):
    """The whole container killed mid-job: nothing removes the horse's gauge file. Docker
    keeps a restarted container's writable layer, so a directory there would reread it;
    the compose tmpfs gives every start an empty one (tests/unit/test_worker_metrics.py)."""
    killed = _worker(tmp_path, "quantlab", _job({"die": "worker"}))
    assert killed.returncode < 0  # SIGKILLed mid-job
    assert any(p.name.startswith("gauge_livesum") for p in tmp_path.iterdir())
    reused = _worker(tmp_path, "none")  # a restart into the same directory: the defect
    assert _running(reused.stdout) == 1
    restarted = _worker(tmp_path / "fresh", "none")  # what the tmpfs gives each start
    assert restarted.returncode == 0, restarted.stderr[-2000:]
    assert _running(restarted.stdout) == 0
