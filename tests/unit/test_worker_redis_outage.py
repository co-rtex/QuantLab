"""A worker keeps going when Redis is unreachable (#185); see the integration test for a
real forking worker through an outage mid-job."""

from __future__ import annotations

from typing import Any

import fakeredis
import pytest
import redis
from rq import Queue, Worker

from quantlab_api.jobs import RQ_OPTIONS
from quantlab_api.worker import QuantlabWorker

DOWN = [redis.exceptions.ConnectionError("refused"), redis.exceptions.TimeoutError("timed out")]


def worker() -> QuantlabWorker:
    conn = fakeredis.FakeRedis()
    return QuantlabWorker(
        [Queue("q", connection=conn, **RQ_OPTIONS)], connection=conn, name="w", **RQ_OPTIONS
    )  # type: ignore[arg-type]


def failing(exc: Exception) -> Any:
    def f(*_: object, **__: object) -> None:
        raise exc

    return f


@pytest.mark.parametrize("exc", DOWN, ids=["connection", "timeout"])
@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("heartbeat", ()),
        ("maintain_heartbeats", (None, None)),
        ("run_maintenance_tasks", ()),
        ("check_for_suspension", (False,)),
        ("set_state", ("idle",)),
        ("register_death", ()),
    ],
)
def test_an_unreachable_redis_does_not_stop_the_worker(
    monkeypatch: pytest.MonkeyPatch, method: str, args: tuple, exc: Exception
) -> None:
    """These are what quit a worker in an outage: the horse monitor's heartbeat, the
    loop's heartbeat after a job, periodic maintenance, the suspension check at the top of
    every loop pass, the idle state the dequeue writes before its own reconnecting loop,
    and the death a stopping worker registers."""
    monkeypatch.setattr(Worker, method, failing(exc))
    getattr(worker(), method)(*args)  # logged, not raised


ARGS = {
    "maintain_heartbeats": (None, None),
    "check_for_suspension": (False,),
    "set_state": ("idle",),
}


@pytest.mark.parametrize(
    "method",
    [
        "heartbeat",
        "maintain_heartbeats",
        "run_maintenance_tasks",
        "check_for_suspension",
        "set_state",
        "register_death",
    ],
)
def test_any_other_error_still_propagates(monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    monkeypatch.setattr(Worker, method, failing(ValueError("a bug")))
    with pytest.raises(ValueError, match="a bug"):
        getattr(worker(), method)(*ARGS.get(method, ()))


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


def test_a_stop_request_during_a_job_still_stops_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rq.exceptions import StopRequested

    monkeypatch.setattr(Worker, "execute_job", failing(StopRequested()))
    with pytest.raises(StopRequested):
        worker().execute_job(None, None)
