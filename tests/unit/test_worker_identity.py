"""A restarted worker can rejoin at once (#162).

RQ refuses a worker name whose registration has no recorded death, until it expires
(``worker_ttl`` + 60 s). An unclean exit (a kill, an OOM kill, a crash) records none, and a
restarted container keeps its hostname and runs the worker as pid 1, so naming workers by
host and pid made the new process exit for about 8 minutes.
"""

from __future__ import annotations

import os

import fakeredis
import pytest
from rq import Queue, Worker

from quantlab_api import jobs as J
from quantlab_api.jobs import RQ_OPTIONS


@pytest.fixture
def container(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A process in a container: the same hostname every start, and pid 1."""
    monkeypatch.delenv("QUANTLAB_WORKER_NAME", raising=False)
    monkeypatch.setattr(J.socket, "gethostname", lambda: "71de78d58ae0")
    monkeypatch.setattr(J.os, "getpid", lambda: 1)
    monkeypatch.setattr(J, "_identity", None)
    return monkeypatch


def start(conn: fakeredis.FakeRedis, name: str) -> None:
    """What ``quantlab-worker`` does on start, up to taking work."""
    Worker(
        [Queue("q", connection=conn, **RQ_OPTIONS)], connection=conn, name=name, **RQ_OPTIONS
    ).register_birth()  # type: ignore[arg-type]


def test_a_restart_after_an_unclean_exit_rejoins_under_a_new_name(
    container: pytest.MonkeyPatch,
) -> None:
    conn = fakeredis.FakeRedis()
    first = J.worker_identity()
    start(conn, first)  # then killed: no death is recorded

    container.setattr(J, "_identity", None)  # the restarted process: same host, pid 1
    second = J.worker_identity()
    assert second != first
    start(conn, second)  # does not raise

    assert first.startswith("71de78d58ae0-1-") and second.startswith("71de78d58ae0-1-")
    assert ":" not in first + second


def test_the_old_host_and_pid_name_is_refused_after_an_unclean_exit() -> None:
    """The defect itself, so the test above shows something: the same name is refused."""
    conn = fakeredis.FakeRedis()
    start(conn, "71de78d58ae0-1")
    with pytest.raises(ValueError, match="There exists an active worker named"):
        start(conn, "71de78d58ae0-1")


def test_the_name_is_fixed_for_the_process(container: pytest.MonkeyPatch) -> None:
    name = J.worker_identity()
    assert J.worker_identity() == name


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
# The child only writes to a pipe and exits, so the multi-threaded fork warning is moot.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
def test_a_forked_work_horse_reports_the_workers_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """RQ runs each job in a forked child; the job's worker_id is the worker's name."""
    monkeypatch.delenv("QUANTLAB_WORKER_NAME", raising=False)
    monkeypatch.setattr(J, "_identity", None)
    name = J.worker_identity()
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:  # the work horse
        try:
            os.write(write, J.worker_identity().encode())
        finally:
            os._exit(0)
    os.close(write)
    child = os.read(read, 256).decode()
    os.close(read)
    os.waitpid(pid, 0)
    assert child == name


def test_a_long_hostname_still_gives_a_name_the_database_can_hold(
    container: pytest.MonkeyPatch,
) -> None:
    """Linux allows 64-character hostnames, other systems longer; the default never fails."""
    container.setattr(J.socket, "gethostname", lambda: "h:" + "x" * 250)
    name = J.worker_identity()
    assert len(name) <= 128 and ":" not in name and name.startswith("h-xxx")


def test_quantlab_worker_name_overrides_the_default(container: pytest.MonkeyPatch) -> None:
    container.setenv("QUANTLAB_WORKER_NAME", "worker-blue")
    assert J.worker_identity() == "worker-blue"


@pytest.mark.parametrize("bad", ["host:1", "w" * 129])
def test_an_override_rq_or_the_database_cannot_hold_is_refused(
    container: pytest.MonkeyPatch, bad: str
) -> None:
    container.setenv("QUANTLAB_WORKER_NAME", bad)
    with pytest.raises(ValueError, match="QUANTLAB_WORKER_NAME"):
        J.worker_identity()


def test_quantlab_worker_registers_under_worker_identity(container: pytest.MonkeyPatch) -> None:
    from quantlab_api import worker

    built: dict[str, object] = {}

    class Recorder:
        def __init__(self, queues: object, **kwargs: object) -> None:
            built.update(kwargs)

        def work(self, **_: object) -> None:
            return None

    container.setattr(worker, "Worker", Recorder)
    container.setattr(worker, "redis_conn", fakeredis.FakeRedis)
    container.setattr(
        worker, "get_queue", lambda: Queue("q", connection=fakeredis.FakeRedis(), **RQ_OPTIONS)
    )  # type: ignore[arg-type]
    container.setattr(worker, "setup_tracing", lambda *a, **k: None)
    worker.main()
    assert built["name"] == J.worker_identity()
    assert str(built["name"]).startswith("71de78d58ae0-1-")
