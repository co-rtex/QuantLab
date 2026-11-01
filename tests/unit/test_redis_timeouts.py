"""The API's Redis client has explicit timeouts, and a worker's keeps RQ's own (#200).

A Redis that accepts connections but never answers (paused, overloaded, a network that
drops replies) fails one call in about the reply timeout: redis-py does not retry it,
since a client made by ``from_url`` has a pool and pool connections retry nothing. Each
API caller's direction on that failure: tests/integration/test_redis_timeouts.py.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import redis
from rq import Queue

from quantlab_api.config import Settings
from quantlab_api.jobs import RQ_OPTIONS


@contextmanager
def silent_redis() -> Iterator[str]:
    """The URL of a server that completes TCP connections (the kernel does, from the
    listen backlog) and never reads or answers: a hung Redis, as a client sees one."""
    srv = socket.create_server(("127.0.0.1", 0), backlog=64)
    try:
        yield f"redis://127.0.0.1:{srv.getsockname()[1]}/0"
    finally:
        srv.close()


def api_redis(monkeypatch: pytest.MonkeyPatch, **settings: object) -> redis.Redis:
    from quantlab_api import jobs

    s = Settings(_env_file=None, **settings)  # type: ignore[arg-type]
    monkeypatch.setattr(jobs, "get_settings", lambda: s)
    monkeypatch.setattr(jobs, "_redis", None)
    return jobs.redis_conn()


def _timeouts(conn: redis.Redis) -> tuple[object, object]:
    kw = conn.connection_pool.connection_kwargs
    return kw.get("socket_connect_timeout"), kw.get("socket_timeout")


def test_the_api_client_sets_its_timeouts_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """In the client's own settings, not inherited from redis-py's defaults."""
    assert _timeouts(api_redis(monkeypatch)) == (5.0, 5.0)
    configured = api_redis(
        monkeypatch, redis_connect_timeout_seconds=2, redis_socket_timeout_seconds=0.5
    )
    assert _timeouts(configured) == (2.0, 0.5)


def test_a_redis_that_never_answers_fails_one_call_in_about_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with silent_redis() as url:
        conn = api_redis(
            monkeypatch,
            redis_url=url,
            redis_connect_timeout_seconds=0.3,
            redis_socket_timeout_seconds=0.3,
        )
        t = time.monotonic()
        with pytest.raises(redis.exceptions.TimeoutError):
            conn.get("ql:probe")
        elapsed = time.monotonic() - t
    # One attempt, not redis-py's 1 + 10 retries of a pool-less client (3.3 s and more).
    assert 0.25 <= elapsed < 1.0, elapsed


def test_a_worker_keeps_rq_s_own_reply_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """RQ raises a worker connection's reply timeout above its blocking dequeue's: an
    explicit 5 s must not cut that dequeue short (worker.py, docs/RUNBOOK.md)."""
    from quantlab_api.worker import QuantlabWorker

    conn = api_redis(monkeypatch, redis_url="redis://127.0.0.1:1/0")  # never contacted
    w = QuantlabWorker(
        [Queue("q", connection=conn, **RQ_OPTIONS)],  # type: ignore[arg-type]
        connection=conn,
        name="w",
        **RQ_OPTIONS,  # type: ignore[arg-type]
    )
    assert w.connection_timeout == 415  # dequeue timeout (worker_ttl 420 - 15) + 10
    assert _timeouts(conn) == (5.0, 415)
