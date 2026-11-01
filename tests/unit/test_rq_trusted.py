"""The job queue cannot be used to run code on a worker (#115).

Anyone who can write to Redis can put entries on the queue. With RQ's defaults that is
code execution on a worker, which holds the database URL, the JWT key and the data
volume: a pickled payload runs while it is unpickled, and a callable named by string is
imported and called. Each test shows the payload does run on a default worker, so the
refusal is meaningful, and is not run by ours.
"""

from __future__ import annotations

import os
import pickle
import uuid
from pathlib import Path

import fakeredis
import pytest
from rq import Queue, SimpleWorker
from rq.callbacks import Callback
from rq.job import Job, JobStatus
from rq.serializers import JSONSerializer
from rq.webhook import Webhook

from quantlab_api.jobs import RQ_OPTIONS, TRUSTED_FUNC, TrustedJob, UntrustedJobError


class Touch:
    """Creates a file when unpickled: the shape of a pickle exploit, harmlessly."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __reduce__(self) -> tuple[object, tuple[object, ...]]:
        return (Path.touch, (self.path,))


def noop(*_: object) -> None:
    return None


def mark(job: Job, *_: object) -> None:
    """A failure callback that leaves a file behind if it runs."""
    Path(job.meta["marker"]).touch()


def run(conn: fakeredis.FakeRedis, name: str, **options: object) -> None:
    SimpleWorker([Queue(name, connection=conn, **options)], connection=conn, **options).work(
        burst=True
    )  # type: ignore[arg-type]


def test_a_pickled_payload_is_not_executed(tmp_path: Path) -> None:
    conn = fakeredis.FakeRedis()
    marker = tmp_path / "pwned"
    # An attacker's entry: pickled data that touches a file as it is unpickled.
    Queue("attack", connection=conn).enqueue(noop, Touch(marker))

    run(conn, "attack", **RQ_OPTIONS)
    assert not marker.exists()

    # The same entry on a default (pickle) worker runs the payload.
    Queue("control", connection=conn).enqueue(noop, Touch(marker))
    run(conn, "control")
    assert marker.exists()


def test_a_callable_named_by_string_is_not_imported_or_called(tmp_path: Path) -> None:
    conn = fakeredis.FakeRedis()
    target = tmp_path / "made-by-os-mkdir"
    # Valid JSON, naming another callable: JSON alone does not stop this.
    job = Queue("attack", connection=conn, serializer=JSONSerializer).enqueue(
        "os.mkdir", str(target)
    )

    run(conn, "attack", **RQ_OPTIONS)
    assert not target.exists()
    assert (
        Job.fetch(job.id, connection=conn, serializer=JSONSerializer).get_status()
        == JobStatus.FAILED
    )

    Queue("control", connection=conn, serializer=JSONSerializer).enqueue("os.mkdir", str(target))
    run(conn, "control", serializer=JSONSerializer)
    assert target.is_dir()
    os.rmdir(target)


@pytest.mark.parametrize(
    ("func", "args", "kwargs"),
    [
        ("os.system", ("true",), {}),
        (TRUSTED_FUNC, ("not-a-uuid",), {}),
        (TRUSTED_FUNC, (str(uuid.uuid4()), "extra"), {}),
        (TRUSTED_FUNC, (str(uuid.uuid4()),), {"worker_id": "w"}),
        (TRUSTED_FUNC, (), {}),
    ],
)
def test_only_execute_job_with_one_job_id_is_trusted(func: str, args: tuple, kwargs: dict) -> None:
    conn = fakeredis.FakeRedis()
    job = Queue("q", connection=conn, **RQ_OPTIONS).enqueue(func, *args, **kwargs)  # type: ignore[arg-type]
    fetched = TrustedJob.fetch(job.id, connection=conn, serializer=JSONSerializer)
    with pytest.raises(UntrustedJobError):
        _ = fetched.func


def test_the_real_job_is_trusted() -> None:
    from quantlab_api.jobs import execute_job

    conn = fakeredis.FakeRedis()
    job = Queue("q", connection=conn, **RQ_OPTIONS).enqueue(execute_job, str(uuid.uuid4()))  # type: ignore[arg-type]
    assert job.func_name == TRUSTED_FUNC
    fetched = TrustedJob.fetch(job.id, connection=conn, serializer=JSONSerializer)
    assert fetched.func is execute_job
    # Stored as JSON, not pickle. (Unpickling here is deliberate and safe: the bytes are
    # the JSON this test just wrote, and the point is that pickle cannot read them.)
    raw = conn.hget(f"rq:job:{job.id}", "data")
    assert raw is not None
    with pytest.raises(pickle.UnpicklingError):
        pickle.loads(raw)


def test_a_failure_callback_from_the_queue_is_not_run(tmp_path: Path) -> None:
    """Callbacks are imported by name too: an entry that fails on purpose must not get
    its failure callback run (found by the automated review of #115)."""
    conn = fakeredis.FakeRedis()
    marker = tmp_path / "callback-ran"

    def entry(queue: str, **options: object) -> None:
        Queue(queue, connection=conn, serializer=JSONSerializer, **options).enqueue(  # type: ignore[arg-type]
            "os.listdir",
            str(tmp_path / "missing"),
            on_failure=Callback(mark),
            meta={"marker": str(marker)},
        )

    entry("attack")
    run(conn, "attack", **RQ_OPTIONS)
    assert not marker.exists()

    entry("control")
    run(conn, "control", serializer=JSONSerializer)
    assert marker.exists()


def test_callbacks_and_webhooks_from_the_queue_are_dropped() -> None:
    from quantlab_api.jobs import execute_job

    conn = fakeredis.FakeRedis()
    job = Queue("q", connection=conn, **RQ_OPTIONS).enqueue(  # type: ignore[arg-type]
        execute_job,
        str(uuid.uuid4()),
        on_success=Callback(noop),
        on_failure=Callback(noop),
        on_stopped=Callback(noop),
        webhooks=[Webhook("http://127.0.0.1:9/exfiltrate", "failed")],
    )
    stored = Job.fetch(job.id, connection=conn, serializer=JSONSerializer)
    assert stored.webhooks and stored.failure_callback is not None  # they are in Redis

    trusted = TrustedJob.fetch(job.id, connection=conn, serializer=JSONSerializer)
    assert trusted.webhooks == []
    assert trusted.success_callback is None
    assert trusted.failure_callback is None
    assert trusted.stopped_callback is None


def test_the_worker_uses_the_trusted_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """``quantlab-worker`` builds its RQ worker with RQ_OPTIONS: without them it unpickles."""
    from quantlab_api import worker

    built: dict[str, object] = {}

    class Recorder:
        def __init__(self, queues: object, **kwargs: object) -> None:
            built.update(kwargs)

        def work(self, **_: object) -> None:
            return None

    monkeypatch.setattr(worker, "QuantlabWorker", Recorder)
    monkeypatch.setattr(worker, "redis_conn", fakeredis.FakeRedis)
    monkeypatch.setattr(
        worker, "get_queue", lambda: Queue("q", connection=fakeredis.FakeRedis(), **RQ_OPTIONS)
    )  # type: ignore[arg-type]
    monkeypatch.setattr(worker, "setup_tracing", lambda *a, **k: None)
    worker.main()
    assert built["serializer"] is JSONSerializer
    assert built["job_class"] is TrustedJob


def test_registry_cleanup_does_not_run_a_failure_callback(tmp_path: Path) -> None:
    """Worker maintenance fails abandoned jobs and runs their failure callbacks (rq's
    StartedJobRegistry.cleanup): through our queue's registry, no callback runs."""
    conn = fakeredis.FakeRedis()
    marker = tmp_path / "cleanup-callback-ran"

    def abandoned(queue: Queue) -> None:
        job = Queue(queue.name, connection=conn, serializer=JSONSerializer).enqueue(
            "os.getcwd", on_failure=Callback(mark), meta={"marker": str(marker)}
        )
        job.set_status(JobStatus.STARTED)
        # Keyed as RQ 3's worker keys it, "<job id>:<execution id>" (a bare id is a legacy
        # form RQ warns about, #137); the score says it expired long ago.
        conn.zadd(queue.started_job_registry.key, {f"{job.id}:lost-execution": 1})

    ours = Queue("attack", connection=conn, **RQ_OPTIONS)  # type: ignore[arg-type]
    abandoned(ours)
    ours.started_job_registry.cleanup()
    assert not marker.exists()

    control = Queue("control", connection=conn, serializer=JSONSerializer)
    abandoned(control)
    control.started_job_registry.cleanup()
    assert marker.exists()


def test_a_scheduled_entry_is_refused_once_it_is_due(tmp_path: Path) -> None:
    """The scheduler moves due entries onto the queue with RQ's base Job (rq.scheduler):
    it runs nothing itself, and our worker refuses what it moved."""
    from datetime import UTC, datetime, timedelta

    from rq.scheduler import RQScheduler

    conn = fakeredis.FakeRedis()
    target = tmp_path / "made-by-a-scheduled-entry"
    past = datetime.now(UTC) - timedelta(seconds=5)

    def schedule(name: str) -> None:
        queue = Queue(name, connection=conn, serializer=JSONSerializer)
        queue.enqueue_at(past, "os.mkdir", str(target))
        scheduler = RQScheduler([name], connection=conn, serializer=JSONSerializer)
        # The registries it moves from, as acquire_locks() would set them (fakeredis has
        # no Lua scripting for the lock itself).
        scheduler.prepare_registries([name])
        scheduler.enqueue_scheduled_jobs()
        assert len(Queue(name, connection=conn, serializer=JSONSerializer)) == 1  # it moved

    schedule("attack")
    assert not target.exists()  # moving it ran nothing
    run(conn, "attack", **RQ_OPTIONS)
    assert not target.exists()

    schedule("control")
    run(conn, "control", serializer=JSONSerializer)
    assert target.is_dir()
