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
from tests.limiter_clock import limiter_clock  # noqa: F401 - the fixture below
from tests.unit.test_redis_timeouts import api_redis, silent_redis

# Fixed-window limits: counted against a frozen clock, never across a window boundary.
pytestmark = pytest.mark.usefixtures("limiter_clock")


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
    """Counted in Redis commands, not seconds: the first call waits its timeout and opens
    the breaker, and no other call asks Redis (each used to wait a timeout of its own)."""
    import redis

    commands: list[str] = []
    real = redis.Redis.execute_command

    def counted(self: redis.Redis, *args: Any, **kwargs: Any) -> Any:
        commands.append(str(args[0]))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(redis.Redis, "execute_command", counted)
    with silent_redis() as url:
        api_redis(
            monkeypatch,
            redis_url=url,
            redis_connect_timeout_seconds=0.3,
            redis_socket_timeout_seconds=0.3,
        )
        lim = RateLimiter()
        # A login's calls (an address check, an account reserve, a refund), then more.
        results = [lim.reserve("ip", 2), lim.reserve_receipt("acct", 30), lim.reserve("ip", 2)]
        lim.refund(results[1])  # type: ignore[arg-type]
        with pytest.raises(ApiError) as refused:
            lim.check("ip", 2)
    assert [bool(r) for r in results] == [True, True, True]
    assert (refused.value.status, refused.value.code) == (429, "rate_limited")  # still limits
    assert commands == ["INCRBY"]


def test_while_open_redis_is_not_asked(flaky: Flaky) -> None:
    lim = RateLimiter()
    assert lim.reserve("k", 1) is True
    assert flaky.calls == 1
    assert [lim.reserve("k", 1) for _ in range(3)] == [False, False, False]  # local count
    lim.refund(lim.reserve_receipt("k", 1))
    assert lim.count("k") == 5
    assert flaky.calls == 1


def test_a_refund_goes_back_only_where_the_attempt_was_counted(
    flaky: Flaky, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#211 review N2: a refund that was the breaker's probe sent DECR to Redis for an
    attempt counted locally, leaving the bucket at -1, and with no TTL."""
    lim = RateLimiter()
    monkeypatch.setattr(lim, "BREAKER_SECONDS", 0.05)
    local = lim.reserve_receipt("acct", 5)  # Redis down: counted locally
    assert local and not local.in_redis
    flaky.down = False
    time.sleep(0.06)  # the breaker's window is over: the next call would be its probe
    lim.refund(local)
    assert flaky.real.exists(local.bucket) == 0  # no DECR: not created at -1
    counted = lim.reserve_receipt("acct", 5)  # the probe; Redis counts it
    assert counted.in_redis and int(flaky.real.get(counted.bucket)) == 1
    lim.refund(counted)
    assert int(flaky.real.get(counted.bucket)) == 0
    # A bucket Redis has lost (a flush, or its TTL at the window's end) is not left at -1.
    again = lim.reserve_receipt("acct", 5)
    flaky.real.flushall()
    lim.refund(again)
    assert int(flaky.real.get(again.bucket) or 0) == 0
    assert 0 < flaky.real.ttl(again.bucket) <= 61


def test_the_floor_never_lowers_a_local_count(
    flaky: Flaky, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#211 review N4: a count from Redis lower than the shard's local one (counted by
    per-call fallbacks, or by another key sharing the shard) must not replace it."""
    import redis

    flaky.down = False
    lim = RateLimiter()
    with monkeypatch.context() as m:
        m.setattr(flaky.real, "incr", lambda *_: (_ for _ in ()).throw(redis.ResponseError("x")))
        assert [lim.reserve("k", 5) for _ in range(5)] == [True] * 5  # local only, 1..5
    assert lim._open_until == 0.0
    assert lim.reserve("k", 5) is True  # Redis answers 1: the floor stays 5
    flaky.down = True
    assert lim.reserve("k", 5) is False  # the 6th locally (an assigned floor gave 2)


def test_one_probe_among_concurrent_calls(flaky: Flaky, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    lim = RateLimiter()
    monkeypatch.setattr(lim, "BREAKER_SECONDS", 0.05)
    lim.reserve("k", 5)
    time.sleep(0.06)
    start, got = threading.Barrier(16), []

    def call() -> None:
        start.wait()
        got.append(lim._redis())

    threads = [threading.Thread(target=call) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got.count(flaky) == 1 and got.count(None) == 15


def test_the_gauge_follows_the_breaker_under_concurrent_transitions(flaky: Flaky) -> None:
    """#211 review N3: the gauge is written under the lock that decides each transition,
    so interleaved opens and closes cannot leave it at 1 with the breaker closed."""
    import threading

    lim = RateLimiter()

    def churn(i: int) -> None:
        for _ in range(500):
            if i % 2:
                lim._redis_failed(ConnectionError("down"))
            else:
                lim._redis_answered()

    threads = [threading.Thread(target=churn, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _gauge() == (1.0 if lim._open_until else 0.0)
    lim._redis_answered()
    assert _gauge() == 0.0


def test_a_new_window_starts_a_local_count_over(flaky: Flaky, limiter_clock) -> None:  # noqa: F811
    """Intended (fixed windows, ADR-0007), and the reason every test that counts within a
    window freezes the clock: #232's CI run straddled a minute and saw `True is False`."""
    lim = RateLimiter()
    assert [lim.reserve("victim", 2) for _ in range(3)] == [True, True, False]
    limiter_clock.advance(29)  # still this minute (the clock starts mid-minute)
    assert lim.reserve("victim", 2) is False
    limiter_clock.advance(1)  # the next minute's window
    assert lim.reserve("victim", 2) is True


def test_in_degraded_mode_the_limit_is_per_process(flaky: Flaky) -> None:
    """Two API processes, each its own limiter. Documented, not fixed: degraded mode
    limits per process (ADR-0007), and a process starts a key it has not seen this window
    from 0, whatever Redis counted in another."""
    flaky.down = False
    a, b = RateLimiter(), RateLimiter()
    assert [a.reserve("acct", 3) for _ in range(3)] == [True] * 3  # in Redis, a's floor 3
    flaky.down = True
    assert a.reserve("acct", 3) is False  # a: from its floor
    assert [b.reserve("acct", 3) for _ in range(4)] == [True, True, True, False]  # b: from 0


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
