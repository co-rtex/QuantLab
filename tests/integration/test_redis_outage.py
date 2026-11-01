"""A busy worker outlasts a Redis outage, and the job loses no attempt (#185).

Before, RQ's horse monitor heartbeat raised ConnectionError and the worker quit: in a
container its running horse died with it, the job lost an attempt, and the container
crash-looped until Redis returned. Here a real forking worker (redis_outage_child.py) runs
queued jobs, with an outage from the middle of the first until after it ends.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
import warnings
from datetime import datetime
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


def _times(jid: uuid.UUID) -> tuple[datetime | None, datetime | None]:
    with SessionLocal() as db:
        job = db.get(Job, jid)
        return job.attempt_started_at, job.finished_at


def _start(
    flag: Path, kind: str, interval: int, mode: str, *jobs: uuid.UUID, log: Path | None = None
) -> subprocess.Popen[str]:
    """The worker, its output piped, or written to `log` when given."""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    out = log.open("w") if log else subprocess.PIPE
    try:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.integration.redis_outage_child",
                str(flag),
                kind,
                str(interval),
                mode,
                *map(str, jobs),
            ],
            cwd=ROOT,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            text=True,
        )
    finally:
        if log:
            out.close()  # the worker has its own copy


def _wait_for(flag: Path) -> None:
    deadline = time.monotonic() + 60
    while not flag.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert flag.exists(), "the outage never started"


def _run(
    kind: str, tmp_path: Path
) -> tuple[subprocess.CompletedProcess[str], uuid.UUID, uuid.UUID]:
    flag = tmp_path / "redis-down"
    first, second = _job({"outage": True}), _job({})
    proc = _start(flag, kind, 1, "burst", first, second)
    # The outage starts inside job 1; end it a few seconds after job 1 is recorded done,
    # so RQ's bookkeeping after the horse also met a dead Redis.
    _wait_for(flag)
    deadline = time.monotonic() + 60
    while _status(first)[0] not in ("completed", "failed") and time.monotonic() < deadline:
        time.sleep(0.2)
    time.sleep(3)
    flag.unlink()
    out, _ = proc.communicate(timeout=90)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, ""), first, second


def _row(jid: uuid.UUID) -> dict[str, object]:
    with SessionLocal() as db:
        job = db.get(Job, jid)
        cols = ("status", "attempts", "message", "error", "worker_id", "attempt_started_at")
        out = {c: getattr(job, c) for c in cols}
        out |= {c: getattr(job, c) for c in ("heartbeat_at", "finished_at", "lease_expires_at")}
        return {k: (v.value if hasattr(v, "value") else v) for k, v in out.items()}


def _settled(jid: uuid.UUID, seconds: float) -> tuple[tuple[str, int], float]:
    """The job's (status, attempts) once terminal, or as it stands after ``seconds``, and how
    long that took. #261: one load-heavy run found job 1 still running after the worker had
    exited 0. A late terminal write and a horse that died mid-job look the same in one read;
    a bounded wait tells them apart."""
    t0 = time.monotonic()
    while (state := _status(jid))[0] not in ("completed", "failed", "timed_out", "cancelled"):
        if time.monotonic() - t0 >= seconds:
            break
        time.sleep(0.2)
    return state, time.monotonic() - t0


def test_a_busy_worker_outlasts_a_redis_outage_and_takes_the_next_job(seeded, tmp_path):
    done, first, second = _run("quantlab", tmp_path)
    assert done.returncode == 0, done.stdout[-3000:]
    state, waited = _settled(first, 10)
    # On a mismatch, everything needed to tell the cases apart: whether the terminal state
    # arrived late (waited > 0) or never, job 1's row, and the worker's whole output.
    why = f"waited {waited:.1f}s; job 1 {_row(first)}\n{done.stdout[-12000:]}"
    assert state == ("completed", 1), why  # no attempt lost
    if waited >= 0.5:  # completed, but seen only after the worker had exited: say so (#261)
        warnings.warn(f"#261: job 1's terminal state arrived late: {why[:2000]}", stacklevel=1)
    assert _status(second) == ("completed", 1)  # the same worker went on
    assert "found an unhandled exception, quitting" not in done.stdout
    # The monitor's own heartbeat met the outage while the horse ran, and was the one
    # skipped (#197 review: a catch-all elsewhere could otherwise hide its removal).
    assert "during=job_heartbeat" in done.stdout, done.stdout[-3000:]
    # And the worker took job 2 only after job 1's horse: never two horses at once.
    _, first_done = _times(first)
    second_started, _ = _times(second)
    assert first_done is not None and second_started is not None
    assert second_started >= first_done


def test_rq_s_own_worker_quits_at_the_first_failed_heartbeat(seeded, tmp_path):
    """The control: the defect itself."""
    done, _, second = _run("plain", tmp_path)
    assert "found an unhandled exception, quitting" in done.stdout, done.stdout[-3000:]
    assert _status(second) == (JobStatus.queued.value, 0)  # nobody took the next job


def test_a_sigterm_during_an_outage_waits_for_the_running_job(seeded, tmp_path):
    """`docker stop` during an incident (#197 review). RQ's SIGTERM handler writes the
    shutdown request to Redis; raising there escaped the monitor's wait, and the worker
    tore down with the horse still running (in a container, the horse died with it). With
    a long monitoring interval no heartbeat runs meanwhile: the handler alone is tested."""
    flag = tmp_path / "redis-down"
    job = _job({"outage": True, "seconds": 6})
    log = tmp_path / "worker.log"
    proc = _start(flag, "quantlab", 30, "serve", job, log=log)
    _wait_for(flag)
    time.sleep(1.5)  # mid-job, in the outage
    proc.send_signal(signal.SIGTERM)
    # The worker's own exit, not its output's end: the forked horse holds a copy of the
    # worker's stdout, so communicate() returned only once the horse was done too, however
    # early the worker had left it (#197 review).
    proc.wait(timeout=90)
    # The worker stopped only once the job was done: a worker that left the horse would
    # have exited about 4 s before it finished.
    status_at_exit = _status(job)
    flag.unlink()
    out = log.read_text()
    assert status_at_exit == ("completed", 1), out[-3000:]
    assert proc.returncode == 0, out[-3000:]
    assert "during=shutdown_request" in out, out[-3000:]
