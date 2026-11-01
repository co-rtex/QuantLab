"""A work horse killed for exceeding the worker's memory is recorded as such (#163).

Before, it surfaced minutes later as ``LeaseExpired`` ("worker stopped heartbeating") and
was retried into the same wall. The cgroup files are faked; the job rows are real.
"""

from __future__ import annotations

import os
import signal
import socket
import uuid
from types import SimpleNamespace

import pytest

from tests.integration.conftest import run_pending_jobs
from tests.integration.test_api_workflow import submit

LIMIT = 1536 * 2**20  # the compose worker limit, in bytes


@pytest.fixture
def cgroup(monkeypatch):
    """Fake cgroup v2 files; ``state["kills"]`` is the oom_kill count."""
    from quantlab_api import jobs

    state = {"kills": 0, "max": str(LIMIT)}

    def read(name: str) -> str | None:
        if name == "memory.events":
            return f"low 0\nhigh 0\nmax 12\noom 1\noom_kill {state['kills']}\n"
        if name == "memory.max":
            return state["max"] + "\n"
        return None

    monkeypatch.setattr(jobs, "_cgroup_read", read)
    monkeypatch.setattr(jobs, "_oom_kills_seen", 0)
    return state


def _running(client, seeded) -> tuple[uuid.UUID, int, str]:
    """A submitted backtest, claimed as a pre-#162 horse would claim it (host:<pid>)."""
    from quantlab_api.jobs import claim

    job_id = uuid.UUID(submit(client, seeded).json()["job"]["id"])
    pid = 424242
    wid = f"{socket.gethostname()}:{pid}"
    assert claim(job_id, wid) is not None
    return job_id, pid, wid


def _killed(job_id: uuid.UUID, pid: int, sig: int = signal.SIGKILL, peak_kb: int = 1_400_000):
    from quantlab_api.jobs import horse_killed

    # waitpid's status for a process killed by a signal is the signal number.
    horse_killed(SimpleNamespace(args=[str(job_id)]), pid, sig, SimpleNamespace(ru_maxrss=peak_kb))


def _job(job_id: uuid.UUID):
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Job

    with SessionLocal() as db:
        return db.get(Job, job_id)


def test_an_oom_killed_horse_fails_its_job_as_resource_exhausted(client, seeded, cgroup):
    job_id, pid, _ = _running(client, seeded)
    cgroup["kills"] = 1  # the kernel killed a process in this container
    _killed(job_id, pid)
    job = _job(job_id)
    assert job.status.value == "failed" and job.message == "failed: out of memory"
    assert job.error["type"] == "ResourceExhausted" and job.error["retryable"] is False
    assert "of the worker's 1536 MB limit" in job.error["message"]
    assert job.lease_expires_at is None and job.finished_at is not None
    h = seeded["headers"]
    exp = client.get(f"/api/v1/jobs/{job_id}", headers=h).json()
    assert exp["error"]["type"] == "ResourceExhausted"  # a safe type: users see the message
    assert "out of memory" in exp["error"]["message"]
    assert run_pending_jobs() == []  # not retried into the same wall


def test_the_attempt_is_matched_by_the_workers_own_name_since_162(client, seeded, cgroup):
    # Since #170 the horse inherits the worker's fixed name (<host>-<pid>-<hex>), so the
    # match no longer depends on the horse's pid.
    from quantlab_api.jobs import claim, worker_identity

    job_id = uuid.UUID(submit(client, seeded).json()["job"]["id"])
    name = worker_identity()
    assert ":" not in name and name.count("-") >= 2
    assert claim(job_id, name) is not None
    cgroup["kills"] = 1
    _killed(job_id, pid=999_999)  # not the claiming pid: only the name can match
    assert _job(job_id).error["type"] == "ResourceExhausted"


def test_a_peak_at_the_memory_limit_counts_without_an_oom_kill_count(client, seeded, cgroup):
    job_id, pid, _ = _running(client, seeded)
    _killed(job_id, pid, peak_kb=LIMIT // 2**10)  # no oom_kill rise, but at the limit
    assert _job(job_id).error["type"] == "ResourceExhausted"


@pytest.mark.parametrize(
    "case",
    ["sigkill_without_oom_evidence", "sigterm", "another_workers_attempt", "no_cgroup"],
)
def test_other_horse_deaths_stay_with_the_reaper(client, seeded, cgroup, monkeypatch, case):
    from quantlab_api import jobs

    job_id, pid, _ = _running(client, seeded)
    sig, killed_pid = signal.SIGKILL, pid
    if case == "sigterm":
        cgroup["kills"], sig = 1, signal.SIGTERM
    elif case == "another_workers_attempt":  # reclaimed since: not this horse's attempt
        cgroup["kills"], killed_pid = 1, pid + 1
    elif case == "no_cgroup":  # macOS or no cgroup v2: no evidence either way
        monkeypatch.setattr(jobs, "_cgroup_read", lambda name: None)
        monkeypatch.setattr(jobs, "_oom_kills_seen", None)
    _killed(job_id, killed_pid, sig=sig, peak_kb=200_000)
    job = _job(job_id)
    assert job.status.value == "running" and job.error is None  # the reaper decides later


def test_each_oom_kill_is_counted_once(client, seeded, cgroup):
    first, pid1, _ = _running(client, seeded)
    cgroup["kills"] = 1
    _killed(first, pid1)
    second = uuid.UUID(submit(client, seeded, name="second").json()["job"]["id"])
    from quantlab_api.jobs import claim

    pid2 = 434343
    assert claim(second, f"{socket.gethostname()}:{pid2}") is not None
    _killed(second, pid2, peak_kb=200_000)  # SIGKILL, but the count did not rise again
    assert _job(first).error["type"] == "ResourceExhausted"
    assert _job(second).status.value == "running"


def test_a_job_that_already_finished_is_left_alone(client, seeded, cgroup):
    sub = submit(client, seeded).json()
    assert run_pending_jobs() == ["completed"]
    cgroup["kills"] = 1
    _killed(uuid.UUID(sub["job"]["id"]), os.getpid())
    assert _job(uuid.UUID(sub["job"]["id"])).status.value == "completed"
