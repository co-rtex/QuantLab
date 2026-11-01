"""A job the reaper requeues is delivered once (#186).

Its requeue enqueued it, and the redelivery pass of the same cycle, which picks queued jobs
whose last delivery or heartbeat is old, saw the lost worker's heartbeat and enqueued it
again: `reaper_cycle requeued=1 redelivered=1`, then `job_skip ... not claimable`. Harmless
(the claim is atomic) but a wasted delivery. Deliveries are counted by patching enqueue.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select, update

from quantlab_api import jobs as J
from quantlab_api.db import SessionLocal
from quantlab_api.models import Job, JobStatus, User, Workspace


def lost_worker_job() -> uuid.UUID:
    """A job claimed an hour ago whose worker died: old heartbeat, expired lease."""
    with SessionLocal() as db:
        user, ws = db.scalar(select(User)), db.scalar(select(Workspace))
        job, _ = J.submit_job(
            db, workspace_id=ws.id, user_id=user.id, kind="t.delivery", payload={}, dispatch=False
        )
        jid = job.id
    assert J.claim(jid, "lost-worker") is not None
    hour_ago = func.now() - timedelta(hours=1)
    with SessionLocal() as db:
        db.execute(
            update(Job)
            .where(Job.id == jid)
            .values(
                created_at=hour_ago,
                attempt_started_at=hour_ago,
                heartbeat_at=func.now() - timedelta(minutes=59),
                lease_expires_at=func.now() - timedelta(minutes=58),
            )
        )
        db.commit()
    return jid


def deliveries(monkeypatch, ok: bool = True) -> list[uuid.UUID]:
    sent: list[uuid.UUID] = []

    def enqueue(job, delay_seconds: float = 0.0, delivery_id: str | None = None) -> bool:
        sent.append(job.id)
        return ok

    monkeypatch.setattr(J, "enqueue", enqueue)
    return sent


def test_one_reap_after_a_lost_worker_delivers_the_job_once(seeded, monkeypatch):
    jid = lost_worker_job()
    sent = deliveries(monkeypatch)
    stats = J.reap()
    assert stats["requeued"] == 1 and stats["redelivered"] == 0
    assert sent == [jid]
    J.reap()  # the next cycle, 15 s later: still within the redelivery cutoff
    assert sent == [jid]
    with SessionLocal() as db:
        assert db.get(Job, jid).status == JobStatus.queued


def test_a_requeue_whose_delivery_failed_is_still_redelivered(seeded, monkeypatch):
    """Redis down: the requeue's enqueue fails and is not recorded as a delivery, so the
    job stays due for redelivery (here the same pass's redelivery tries again)."""
    jid = lost_worker_job()
    sent = deliveries(monkeypatch, ok=False)
    stats = J.reap()
    assert stats["requeued"] == 1
    assert sent == [jid, jid]  # the requeue's delivery, then the redelivery pass's
    with SessionLocal() as db:
        job = db.get(Job, jid)
        assert job.status == JobStatus.queued
        old = db.scalar(select(func.now() - timedelta(minutes=30)))
        assert job.heartbeat_at < old  # never recorded as delivered
    sent_ok = deliveries(monkeypatch)
    J.reap()  # Redis back: delivered, and recorded
    assert sent_ok == [jid]
