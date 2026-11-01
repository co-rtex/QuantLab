"""Heavy request-time work is bounded per process, never queued (#223).

Sixteen concurrent optimizations of 100 symbols drove the Docker VM into a global OOM: the
kernel killed API processes, and could have killed Postgres.
"""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from quantlab_api import limits
from quantlab_api.config import Settings
from quantlab_api.errors import ApiError


@pytest.fixture(autouse=True)
def slots(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def configure(n: int) -> None:
        s = Settings(_env_file=None, heavy_requests_per_process=n)
        monkeypatch.setattr("quantlab_api.config.get_settings", lambda: s)
        limits.reset_heavy_slots()

    yield configure
    limits.reset_heavy_slots()


def _refused(route: str) -> float:
    return (
        REGISTRY.get_sample_value("quantlab_heavy_requests_refused_total", {"route": route}) or 0.0
    )


def test_a_full_process_refuses_at_once_with_retry_after(slots) -> None:  # type: ignore[no-untyped-def]
    slots(2)
    before = _refused("probe")
    with (
        limits.heavy_slot("probe"),
        limits.heavy_slot("probe"),
        pytest.raises(ApiError) as refused,
        limits.heavy_slot("probe"),
    ):
        pytest.fail("a third slot was granted")
    assert (refused.value.status, refused.value.code) == (503, "server_busy")
    assert refused.value.headers == {"Retry-After": "2"}
    assert _refused("probe") == before + 1
    with limits.heavy_slot("probe"):  # released: free again
        pass


def test_a_failing_request_gives_its_slot_back(slots) -> None:  # type: ignore[no-untyped-def]
    slots(1)
    with pytest.raises(ZeroDivisionError), limits.heavy_slot("probe"):
        1 / 0  # noqa: B018
    with limits.heavy_slot("probe"):
        pass


def test_a_refusal_is_immediate_never_a_wait(slots) -> None:  # type: ignore[no-untyped-def]
    """Never queued: a request that waited for a slot would hold its thread meanwhile."""
    import time

    slots(1)
    with limits.heavy_slot("probe"):
        t = time.monotonic()
        with pytest.raises(ApiError), limits.heavy_slot("probe"):
            pass
        assert time.monotonic() - t < 0.2
