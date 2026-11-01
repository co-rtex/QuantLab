"""A controllable clock for the fixed-window rate limiter (``security.RateLimiter``).

Its counts are per ``int(time.time() // window)``, in Redis bucket names and in the local
shard tables alike, so a test that straddles a window boundary sees a count start over:
#232's CI run failed ``test_fallback_limiter_is_bounded_and_atomic_reserve`` with
``assert True is False`` on the slower coverage run. Tests that count within a window use
this clock instead of the wall clock.

Only the module ``quantlab_api.security`` sees it, and only its ``time()``: ``monotonic()``
stays real, so the limiter's breaker (#207) still times out. It starts mid-minute, within
30 s of the real time. Not for a test that compares ``security``'s clock with the real
one to the second, such as a device cookie expired by 1 s (test_device_cookies.py, which
pins its Redis buckets instead).
"""

from __future__ import annotations

import time as _time
from collections.abc import Iterator

import pytest


class LimiterClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    @staticmethod
    def monotonic() -> float:
        return _time.monotonic()

    def advance(self, seconds: float) -> None:
        self.now += seconds


def freeze_limiter_clock(monkeypatch: pytest.MonkeyPatch) -> LimiterClock:
    from quantlab_api import security

    clock = LimiterClock((_time.time() // 60) * 60 + 30)  # mid-minute: no 60 s window ends
    monkeypatch.setattr(security, "time", clock)
    return clock


@pytest.fixture
def limiter_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[LimiterClock]:
    yield freeze_limiter_clock(monkeypatch)
