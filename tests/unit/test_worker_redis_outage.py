"""A worker keeps going when Redis is unreachable (#185); see the integration test for a
real forking worker through an outage mid-job, and through a SIGTERM in one."""

from __future__ import annotations

import os
import signal
from types import SimpleNamespace
from typing import Any

import fakeredis
import pytest
import redis
from rq import Queue, Worker
from rq.exceptions import StopRequested

from quantlab_api.jobs import RQ_OPTIONS
from quantlab_api.worker import QuantlabWorker

JOB = SimpleNamespace(id="job-1")
DOWN = [redis.exceptions.ConnectionError("refused"), redis.exceptions.TimeoutError("timed out")]
REFUSED = [
    redis.exceptions.AuthenticationError("invalid password"),
    redis.exceptions.AuthorizationError("NOPERM"),
]

# Each method the worker guards, with arguments to call it.
GUARDED = [
    ("heartbeat", ()),
    ("maintain_heartbeats", (None, None)),
    ("run_maintenance_tasks", ()),
    ("check_for_suspension", (False,)),
    ("set_state", ("idle",)),
    ("register_death", ()),
    ("set_shutdown_requested_date", ()),
]


def worker(**kwargs: Any) -> QuantlabWorker:
    conn = fakeredis.FakeRedis()
    return QuantlabWorker(
        [Queue("q", connection=conn, **RQ_OPTIONS)],  # type: ignore[arg-type]
        connection=conn,
        name="w",
        **RQ_OPTIONS,  # type: ignore[arg-type]
        **kwargs,
    )


def failing(exc: BaseException) -> Any:
    def f(*_: object, **__: object) -> None:
        raise exc

    return f


@pytest.mark.parametrize("exc", DOWN, ids=["connection", "timeout"])
@pytest.mark.parametrize(("method", "args"), GUARDED, ids=[m for m, _ in GUARDED])
def test_an_unreachable_redis_does_not_stop_the_worker(
    monkeypatch: pytest.MonkeyPatch, method: str, args: tuple, exc: Exception
) -> None:
    """These are what quit a worker in an outage: the horse monitor's heartbeat, the
    loop's heartbeat after a job, periodic maintenance, the suspension check at the top of
    every loop pass, the idle state the dequeue writes before its own reconnecting loop,
    the death a stopping worker registers, and the shutdown request a SIGTERM writes."""
    monkeypatch.setattr(Worker, method, failing(exc))
    getattr(worker(), method)(*args)  # logged, not raised


@pytest.mark.parametrize("exc", REFUSED, ids=["authentication", "authorization"])
@pytest.mark.parametrize(("method", "args"), GUARDED, ids=[m for m, _ in GUARDED])
def test_a_refused_password_still_stops_the_worker(
    monkeypatch: pytest.MonkeyPatch, method: str, args: tuple, exc: Exception
) -> None:
    """Subclasses of ConnectionError, but a configuration error: a rotated password must
    fail loudly, not be logged as an outage forever."""
    monkeypatch.setattr(Worker, method, failing(exc))
    with pytest.raises(type(exc)):
        getattr(worker(), method)(*args)


@pytest.mark.parametrize(("method", "args"), GUARDED, ids=[m for m, _ in GUARDED])
def test_any_other_error_still_propagates(
    monkeypatch: pytest.MonkeyPatch, method: str, args: tuple
) -> None:
    monkeypatch.setattr(Worker, method, failing(ValueError("a bug")))
    with pytest.raises(ValueError, match="a bug"):
        getattr(worker(), method)(*args)


@pytest.mark.parametrize("exc", DOWN, ids=["connection", "timeout"])
def test_rq_bookkeeping_after_the_horse_is_skipped_when_redis_is_down(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """After the horse exits, RQ records the job and the worker's state in Redis; the job's
    outcome is already in PostgreSQL."""
    monkeypatch.setattr(Worker, "execute_job", failing(exc))
    w = worker()
    w._last_horse = 0
    w.execute_job(None, None)  # logged, not raised


def test_a_refused_password_during_a_job_stops_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Worker, "execute_job", failing(REFUSED[0]))
    with pytest.raises(redis.exceptions.AuthenticationError):
        worker().execute_job(None, None)


def test_a_stop_request_during_a_job_still_stops_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Worker, "execute_job", failing(StopRequested()))
    with pytest.raises(StopRequested):
        worker().execute_job(None, None)


def test_a_stale_execution_from_a_fork_that_never_happened_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RQ registers the Execution before the pipeline that fails in an outage; left there,
    later horses would report against it."""

    def prepare_then_fail(self: Worker, job: Any, queue: Any) -> None:
        self.executions["stale"] = object()  # type: ignore[assignment]
        raise redis.exceptions.ConnectionError("refused")

    monkeypatch.setattr(Worker, "execute_job", prepare_then_fail)
    w = worker()
    w.execute_job(None, None)
    assert w.executions == {}


def test_a_horse_still_running_is_waited_out_never_left(monkeypatch: pytest.MonkeyPatch) -> None:
    """If an error escapes while the horse runs, the worker waits for it (its lease and
    keepalive are in PostgreSQL) instead of going on, or folding its live metric files."""
    waited: list[int] = []

    def fail_while_running(self: Worker, job: Any, queue: Any) -> None:
        self._horse_pid = 4242
        raise redis.exceptions.ConnectionError("refused")

    def wait(self: Worker) -> tuple[Any, Any, Any]:
        waited.append(self.horse_pid)
        return (4242, 0, None)

    monkeypatch.setattr(Worker, "execute_job", fail_while_running)
    monkeypatch.setattr(Worker, "wait_for_horse", wait)
    w = worker()
    w.execute_job(None, None)
    assert waited == [4242] and w.horse_pid == 0


@pytest.mark.parametrize("exc", REFUSED, ids=["authentication", "authorization"])
def test_a_refused_password_mid_job_waits_for_the_horse_before_stopping(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """A refused password stops the worker, but not by leaving its horse running."""
    waited: list[int] = []

    def fail_while_running(self: Worker, job: Any, queue: Any) -> None:
        self._horse_pid = 4242
        raise exc

    def wait(self: Worker) -> tuple[Any, Any, Any]:
        waited.append(self.horse_pid)
        return (4242, 0, None)

    monkeypatch.setattr(Worker, "execute_job", fail_while_running)
    monkeypatch.setattr(Worker, "wait_for_horse", wait)
    w = worker()
    with pytest.raises(type(exc)):
        w.execute_job(None, None)
    assert waited == [4242] and w.horse_pid == 0


def _killed_in_an_outage(
    monkeypatch: pytest.MonkeyPatch, stat: int, rq_handled: bool
) -> list[tuple]:
    """A horse that exited with `stat`, then RQ's status read failed (Redis down) before or
    after its killed-horse handler."""
    handled: list[tuple] = []

    def run(self: QuantlabWorker, job: Any, queue: Any) -> None:
        self.wait_for_horse()
        if rq_handled:
            self.handle_work_horse_killed(job, 4242, stat, "rusage")
        raise redis.exceptions.ConnectionError("refused")

    monkeypatch.setattr(Worker, "execute_job", run)
    monkeypatch.setattr(Worker, "wait_for_horse", lambda self: (4242, stat, "rusage"))
    w = worker(work_horse_killed_handler=lambda *a: handled.append(a))
    w.execute_job(JOB, None)
    return handled


def test_a_horse_the_monitor_killed_during_an_outage_is_not_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#214: RQ's monitor SIGKILLs a horse that outlives its job timeout + 60 s, and gives
    its killed-horse handler ret_val None. In an outage this worker handed the recorded
    SIGKILL over instead, and one near its memory limit read as out of memory. A kill of
    our own is never an OOM: the reaper times the attempt out from its lease."""
    handled: list[tuple] = []

    def backstop(self: QuantlabWorker, job: Any, queue: Any) -> None:
        self.kill_horse()  # as monitor_work_horse does past timeout + 60 s
        self.wait_for_horse()
        raise redis.exceptions.ConnectionError("refused")  # RQ's status read, Redis down

    monkeypatch.setattr(Worker, "execute_job", backstop)
    monkeypatch.setattr(Worker, "kill_horse", lambda self, sig=signal.SIGKILL: None)
    monkeypatch.setattr(Worker, "wait_for_horse", lambda self: (4242, signal.SIGKILL, "rusage"))
    w = worker(work_horse_killed_handler=lambda *a: handled.append(a))
    w.execute_job(JOB, None)
    assert handled == []
    # The next job's horse is classified as usual: the mark is per job.
    assert _killed_in_an_outage(monkeypatch, signal.SIGKILL, rq_handled=False) != []


def test_a_horse_killed_during_an_outage_is_still_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RQ reads the job's status from Redis before its killed-horse handler (#163's OOM
    classification), so in an outage the handler was never called."""
    assert _killed_in_an_outage(monkeypatch, signal.SIGKILL, rq_handled=False) == [
        (JOB, 4242, signal.SIGKILL, "rusage")
    ]


def test_a_killed_horse_rq_did_hand_over_is_not_classified_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert len(_killed_in_an_outage(monkeypatch, signal.SIGKILL, rq_handled=True)) == 1


def test_a_horse_that_exited_normally_is_not_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _killed_in_an_outage(monkeypatch, os.EX_OK, rq_handled=False) == []
