"""The rate limiter's breaker (#207): after a Redis error it counts in the process for a
while instead of asking Redis on every call, and it never limits less than that.

A hung Redis cost each limiter call a full reply timeout before the fallback, serially:
three per login, 15.21 s measured live with Redis paused (#200's probe).
"""

from __future__ import annotations

import time
from typing import Any

import fakeredis
import pytest
from prometheus_client import REGISTRY

from quantlab_api import jobs, observability
from quantlab_api.errors import ApiError
from quantlab_api.security import RateLimiter
from tests.unit.test_redis_timeouts import api_redis, silent_redis


class Flaky:
    """A Redis that is down until told otherwise, counting every command it is asked."""

    def __init__(self) -> None:
        self.down, self.calls, self.real = True, 0, fakeredis.FakeRedis()

    def __getattr__(self, name: str) -> Any:
        self.calls += 1
        if self.down:
            raise ConnectionError("down")
        return getattr(self.real, name)


@pytest.fixture
def flaky(monkeypatch: pytest.MonkeyPatch) -> Flaky:
    r = Flaky()
    monkeypatch.setattr(jobs, "_redis", r)
    return r


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    events: list[str] = []

    class Recorder:
        def warning(self, event: str, **_: object) -> None:
            events.append(event)

        info = warning

    monkeypatch.setattr(observability, "log", Recorder())
    return events


def _gauge() -> float | None:
    return REGISTRY.get_sample_value("quantlab_rate_limiter_breaker_open", {"limiter": "login"})


def test_a_hung_redis_costs_one_wait_not_one_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    with silent_redis() as url:
        api_redis(
            monkeypatch,
            redis_url=url,
            redis_connect_timeout_seconds=0.3,
            redis_socket_timeout_seconds=0.3,
        )
        lim = RateLimiter()
        t = time.monotonic()
        # A login's calls (an address check, an account reserve, a refund), then more.
        results = [lim.reserve("ip", 2), lim.reserve("acct", 30), lim.reserve("ip", 2)]
        lim.refund("acct")
        with pytest.raises(ApiError) as refused:
            lim.check("ip", 2)
        elapsed = time.monotonic() - t
    assert results == [True, True, True]
    assert (refused.value.status, refused.value.code) == (429, "rate_limited")  # still limits
    assert elapsed < 0.6, elapsed  # one 0.3 s wait; five calls used to wait 1.5 s


def test_while_open_redis_is_not_asked(flaky: Flaky) -> None:
    lim = RateLimiter()
    assert lim.reserve("k", 1) is True
    assert flaky.calls == 1
    assert [lim.reserve("k", 1) for _ in range(3)] == [False, False, False]  # local count
    lim.refund("k")
    assert lim.count("k") == 4
    assert flaky.calls == 1


def test_after_its_window_one_probe_closes_it_on_success(
    flaky: Flaky, monkeypatch: pytest.MonkeyPatch, logged: list[str]
) -> None:
    lim = RateLimiter()
    monkeypatch.setattr(lim, "BREAKER_SECONDS", 0.05)
    lim.reserve("k", 5)
    assert _gauge() == 1.0 and logged == ["rate_limiter_breaker_open"]
    flaky.down = False
    lim.reserve("k", 5)  # still open: counted locally
    assert flaky.calls == 1
    time.sleep(0.06)
    lim.reserve("k", 5)  # the probe: Redis answers and the breaker closes
    assert lim._open_until == 0.0
    assert _gauge() == 0.0 and logged == [
        "rate_limiter_breaker_open",
        "rate_limiter_breaker_closed",
    ]
    lim.reserve("k", 5)
    assert int(flaky.real.get(lim._bucket("k", 60)) or 0) == 2  # both in Redis again


def test_only_one_probe_runs_at_a_time(flaky: Flaky, monkeypatch: pytest.MonkeyPatch) -> None:
    """While the probe waits on Redis, other calls keep counting locally instead of all
    waiting too."""
    lim = RateLimiter()
    monkeypatch.setattr(lim, "BREAKER_SECONDS", 0.05)
    lim.reserve("k", 5)
    time.sleep(0.06)
    assert lim._redis() is flaky  # the probe
    assert lim._redis() is None  # everyone else, meanwhile
    lim._redis_answered()
    assert lim._redis() is flaky


def test_a_failed_probe_opens_it_for_another_window(
    flaky: Flaky, monkeypatch: pytest.MonkeyPatch, logged: list[str]
) -> None:
    lim = RateLimiter()
    monkeypatch.setattr(lim, "BREAKER_SECONDS", 0.05)
    lim.reserve("k", 5)
    time.sleep(0.06)
    lim.reserve("k", 5)  # the probe fails
    assert flaky.calls == 2
    lim.reserve("k", 5)
    assert flaky.calls == 2  # open again
    assert logged == ["rate_limiter_breaker_open"]  # opened once, not once per probe
    assert _gauge() == 1.0


def test_falling_back_never_starts_below_what_redis_counted(flaky: Flaky) -> None:
    """Security review of #207: with the breaker, degraded mode can begin after a single
    error, and its local count started at 0, handing a principal its attempts back. Each
    count Redis returns is now the local floor for that key in this window."""
    flaky.down = False
    lim = RateLimiter()
    assert [lim.reserve("acct", 3) for _ in range(3)] == [True, True, True]  # in Redis
    flaky.down = True
    assert lim.reserve("acct", 3) is False  # the 4th attempt, counted locally from 3
    assert lim.reserve("acct", 3) is False


def test_an_error_that_is_not_an_outage_does_not_open_it(
    flaky: Flaky, monkeypatch: pytest.MonkeyPatch, logged: list[str]
) -> None:
    import redis

    def bad_reply(*_: object) -> int:
        raise redis.exceptions.ResponseError("WRONGTYPE")

    flaky.down = False
    monkeypatch.setattr(flaky.real, "incr", bad_reply)
    lim = RateLimiter()
    assert lim.reserve("k", 5) is True  # that call falls back, as before
    assert lim._open_until == 0.0 and logged == []
    lim.reserve("k", 5)
    assert flaky.calls == 2  # the next call asks Redis again
