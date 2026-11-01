"""The reaper's decisions reach its scraped endpoint, each counted once (#179).

A lease-loss requeue is a retry, and a job the reaper finishes (cancelled, timed out, out of
attempts) is an outcome. Before #179 the reaper counted retries in a process nothing
scraped, and counted none of its outcomes. Counts are read at the endpoint, as deltas: the
counters are process-wide.
"""

from __future__ import annotations

import io
import re
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from quantlab_api import jobs as J
from quantlab_api.db import SessionLocal
from quantlab_api.models import Job, JobStatus, User, Workspace
from quantlab_api.observability import reaper_metrics_app

APP = reaper_metrics_app(None)


def scraped(name: str, **labels: str) -> float:
    environ = {"PATH_INFO": "/metrics", "REQUEST_METHOD": "GET", "wsgi.input": io.BytesIO()}
    text = b"".join(APP(environ, lambda *_: None)).decode()
    for line in text.splitlines():
        m = re.fullmatch(r"([a-zA-Z_:][\w:]*)(?:\{(.*)\})? (\S+)", line)
        if m and m.group(1) == name:
            got = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2) or ""))
            if got == labels:
                return float(m.group(3))
    return 0.0


def retries(kind: str) -> float:
    return scraped("quantlab_job_retries_total", kind=kind, reason="lease_expired")


def outcomes(kind: str, outcome: str) -> float:
    return scraped("quantlab_job_outcomes_total", kind=kind, outcome=outcome)


def a_job(kind: str, **values: object) -> uuid.UUID:
    """A job whose worker was lost: claimed, then its lease ran out."""
    with SessionLocal() as db:
        user, ws = db.scalar(select(User)), db.scalar(select(Workspace))
        job, _ = J.submit_job(
            db, workspace_id=ws.id, user_id=user.id, kind=kind, payload={}, dispatch=False
        )
        jid = job.id
    assert J.claim(jid, "lost-worker") is not None
    with SessionLocal() as db:
        db.execute(
            update(Job)
            .where(Job.id == jid)
            .values(lease_expires_at=func.now() - timedelta(seconds=1), **values)
        )
        db.commit()
    return jid


def test_a_lease_loss_requeue_is_counted_once_at_the_endpoint(seeded):
    kind = f"t.{uuid.uuid4().hex[:8]}"
    a_job(kind)
    assert J.reap()["requeued"] == 1
    assert retries(kind) == 1
    J.reap()  # nothing stale now: nothing more counted
    assert retries(kind) == 1


def test_a_failed_commit_counts_nothing_and_the_next_pass_counts_once(seeded, monkeypatch):
    """Counting before the commit counted a requeue that never happened, and the next pass
    counted it again."""
    kind = f"t.{uuid.uuid4().hex[:8]}"
    jid = a_job(kind)
    real_commit = Session.commit
    calls = {"n": 0}

    def fail_first(self: Session) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected: the commit failed")
        real_commit(self)

    monkeypatch.setattr(Session, "commit", fail_first)
    with pytest.raises(RuntimeError, match="injected"):
        J.reap()
    assert retries(kind) == 0
    with SessionLocal() as db:
        assert db.get(Job, jid).status == JobStatus.running  # rolled back: nothing decided
    monkeypatch.setattr(Session, "commit", real_commit)
    assert J.reap()["requeued"] == 1
    assert retries(kind) == 1


@pytest.mark.parametrize(
    ("values", "outcome"),
    [
        ({"cancel_requested": True}, "cancelled"),
        ({"attempt_started_at": func.now() - timedelta(seconds=2000)}, "timed_out"),
        ({"max_attempts": 1}, "failed"),
    ],
)
def test_an_outcome_the_reaper_decides_is_counted_once(seeded, values, outcome):
    kind = f"t.{uuid.uuid4().hex[:8]}"
    jid = a_job(kind, **values)
    J.reap()
    with SessionLocal() as db:
        assert db.get(Job, jid).status.value == outcome
    assert outcomes(kind, outcome) == 1
    assert retries(kind) == 0
    J.reap()
    assert outcomes(kind, outcome) == 1


def test_a_queued_job_cancelled_before_any_worker_saw_it_is_counted(seeded):
    kind = f"t.{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        user, ws = db.scalar(select(User)), db.scalar(select(Workspace))
        job, _ = J.submit_job(
            db, workspace_id=ws.id, user_id=user.id, kind=kind, payload={}, dispatch=False
        )
        job.cancel_requested = True
        db.commit()
    J.reap()
    assert outcomes(kind, "cancelled") == 1
