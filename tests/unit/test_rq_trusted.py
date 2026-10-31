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
from rq.job import Job, JobStatus
from rq.serializers import JSONSerializer

from quantlab_api.jobs import RQ_OPTIONS, TRUSTED_FUNC, TrustedJob, UntrustedJobError


class Touch:
    """Creates a file when unpickled: the shape of a pickle exploit, harmlessly."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __reduce__(self) -> tuple[object, tuple[object, ...]]:
        return (Path.touch, (self.path,))


def noop(*_: object) -> None:
    return None


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
