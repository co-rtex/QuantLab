"""Each of the API's Redis calls fails in its documented direction, and in about the
timeout, when Redis accepts connections but never answers (#200):

* readiness reports 503 with ``redis: error``;
* the rate limiter counts in the process instead (it still limits, it does not open);
* the bars cache is bypassed and the bars are still served;
* a submitted job stays ``queued`` in PostgreSQL, for the reaper to deliver.

The client itself, and why one call takes one timeout: tests/unit/test_redis_timeouts.py.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select

from quantlab_api.models import Job, JobStatus
from tests.limiter_clock import limiter_clock  # noqa: F401 - the fixture below
from tests.unit.test_redis_timeouts import silent_redis

# Fixed-window limits: counted against a frozen clock, never across a window boundary.
pytestmark = pytest.mark.usefixtures("limiter_clock")

T = 0.3  # both timeouts, seconds
BT = {"start": "2021-01-04", "rebalance": "monthly", "benchmark": "SYNIDX"}


@pytest.fixture
def silent(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The API's own client (jobs.redis_conn), pointed at a Redis that never answers.
    Requested after ``seeded``, whose dataset job needs the usual fake Redis."""
    from quantlab_api import jobs
    from quantlab_api.config import get_settings

    with silent_redis() as url:
        s = get_settings()
        monkeypatch.setattr(s, "redis_url", url)
        monkeypatch.setattr(s, "redis_connect_timeout_seconds", T)
        monkeypatch.setattr(s, "redis_socket_timeout_seconds", T)
        jobs.set_redis(None)
        yield
        jobs.set_redis(None)  # the next test's fixture installs its fake Redis again


def _timed(call):  # type: ignore[no-untyped-def]
    t = time.monotonic()
    out = call()
    return out, time.monotonic() - t


def test_readiness_is_503_with_redis_in_error(client, silent, monkeypatch) -> None:
    import redis.connection

    # One attempt, never a retry: every command the check sends to Redis, counted. It was
    # a wall-clock bound (< 1 + T), which a loaded host broke at 2.3 s (#261).
    sent: list[str] = []
    real = redis.connection.AbstractConnection.send_command

    def spy(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        sent.append(str(args[0]).upper())
        return real(self, *args, **kwargs)

    monkeypatch.setattr(redis.connection.AbstractConnection, "send_command", spy)
    r, elapsed = _timed(lambda: client.get("/api/v1/ready"))
    assert r.status_code == 503, r.text
    assert r.json()["checks"] == {"database": "ok", "redis": "error"}
    assert sent and len(sent) == len(set(sent)), sent  # one attempt: nothing sent twice
    assert elapsed < 10 + 10 * T, elapsed  # a hang guard only, far above any wait


def test_the_rate_limiter_still_limits_counting_in_the_process(client, silent) -> None:
    from quantlab_api.errors import ApiError
    from quantlab_api.security import login_limiter

    login_limiter.check("probe:200", 2)
    login_limiter.check("probe:200", 2)
    with pytest.raises(ApiError) as refused:
        login_limiter.check("probe:200", 2)
    assert (refused.value.status, refused.value.code) == (429, "rate_limited")


def test_a_login_still_succeeds_after_one_redis_wait(client, silent, monkeypatch) -> None:
    """The limiter's first failed call opens its breaker (#207): the rest of this
    registration and login count locally without asking Redis. Before, each call waited
    a timeout: three per login, 15.21 s live."""
    import redis

    from tests.integration.conftest import register

    commands: list[str] = []
    real = redis.Redis.execute_command

    def counted(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        commands.append(str(args[0]))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(redis.Redis, "execute_command", counted)
    register(client)
    r, elapsed = _timed(
        lambda: client.post(
            "/api/v1/auth/login", json={"email": "alice@example.com", "password": "Correct-horse-9"}
        )
    )
    assert r.status_code == 200, r.text
    assert commands == ["INCRBY"], commands  # the registration's first check, then none
    assert elapsed < 1 + T, elapsed


def test_a_device_cookie_login_works_with_the_breaker_open(client, monkeypatch) -> None:
    """A browser that signed in before is throttled per device (#55): with Redis hung,
    its login still succeeds, after one Redis wait, and its locally counted reservation
    is never refunded to Redis (#211 review)."""
    import redis

    from quantlab_api import jobs
    from quantlab_api.config import get_settings
    from tests.integration.conftest import register

    creds = {"email": "alice@example.com", "password": "Correct-horse-9"}
    register(client, **creds)
    assert client.post("/api/v1/auth/login", json=creds).status_code == 200
    assert client.cookies.get("quantlab_device")
    commands: list[str] = []
    real = redis.Redis.execute_command

    def counted(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        commands.append(str(args[0]))
        return real(self, *args, **kwargs)

    with silent_redis() as url:
        s = get_settings()
        monkeypatch.setattr(s, "redis_url", url)
        monkeypatch.setattr(s, "redis_connect_timeout_seconds", T)
        monkeypatch.setattr(s, "redis_socket_timeout_seconds", T)
        jobs.set_redis(None)
        monkeypatch.setattr(redis.Redis, "execute_command", counted)
        r = client.post("/api/v1/auth/login", json=creds)
        jobs.set_redis(None)
    assert r.status_code == 200, r.text
    assert commands == ["INCRBY"], commands  # the device reserve; the refund stays local


def test_bars_are_served_bypassing_the_cache(client, seeded, silent) -> None:
    h, dv = seeded["headers"], seeded["dv"]
    r = client.get(f"/api/v1/dataset-versions/{dv}/bars/SYN001?start=2021-01-01", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["cache"] == "bypass"


def test_a_submitted_job_stays_queued_for_the_reaper(client, seeded, silent) -> None:
    from quantlab_api.db import SessionLocal

    h, ws, dv = seeded["headers"], seeded["ws"], seeded["dv"]
    r = client.post(
        f"/api/v1/workspaces/{ws}/experiments",
        headers=h,
        json={
            "name": "Outage run",
            "dataset_version_id": dv,
            "strategy_key": "equal_weight",
            "config": BT,
        },
    )
    assert r.status_code == 202, r.text
    with SessionLocal() as db:
        job = db.get(Job, uuid.UUID(r.json()["job"]["id"]))
        assert job is not None
        assert (job.status, job.attempts) == (JobStatus.queued, 0)
        assert db.scalar(select(Job.id).where(Job.status == JobStatus.running)) is None
