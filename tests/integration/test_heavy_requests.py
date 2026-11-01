"""The heavy routes refuse rather than stack up (#223): 503 server_busy with Retry-After
when their process has no free slot, and optimize also 429 past its per-user limit."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from quantlab_api import limits
from quantlab_api.config import get_settings
from tests.integration.test_portfolios import _create
from tests.limiter_clock import limiter_clock  # noqa: F401 - the fixture below

# Fixed-window limits: counted against a frozen clock, never across a window boundary.
pytestmark = pytest.mark.usefixtures("limiter_clock")

OPTIMIZE = {"symbols": ["SYN001", "SYN002", "SYN003"], "lookback": 60}


@pytest.fixture
def busy(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """This process's only heavy slot, held by another request."""
    monkeypatch.setattr(get_settings(), "heavy_requests_per_process", 1)
    limits.reset_heavy_slots()
    sem = limits._heavy_semaphore()
    assert sem.acquire(blocking=False)
    yield
    sem.release()
    limits.reset_heavy_slots()


def _assert_busy(r) -> None:  # type: ignore[no-untyped-def]
    assert r.status_code == 503, r.text
    assert r.headers["retry-after"] == "2"
    assert r.json()["error"]["code"] == "server_busy"


def test_optimize_is_refused_while_the_process_is_busy(client, seeded, busy) -> None:
    r = client.post(
        f"/api/v1/dataset-versions/{seeded['dv']}/optimize",
        headers=seeded["headers"],
        json=OPTIMIZE,
    )
    _assert_busy(r)


def test_the_portfolio_ledger_routes_are_refused_while_busy(client, seeded, monkeypatch) -> None:
    pid = _create(client, seeded)  # before the slot is taken
    monkeypatch.setattr(get_settings(), "heavy_requests_per_process", 1)
    limits.reset_heavy_slots()
    sem = limits._heavy_semaphore()
    assert sem.acquire(blocking=False)
    try:
        h = seeded["headers"]
        _assert_busy(client.get(f"/api/v1/portfolios/{pid}/valuation", headers=h))
        _assert_busy(
            client.post(
                f"/api/v1/portfolios/{pid}/transactions",
                headers=h,
                json={"trade_date": "2021-02-01", "kind": "deposit", "amount": 1},
            )
        )
        _assert_busy(
            client.post(
                f"/api/v1/workspaces/{seeded['ws']}/portfolios",
                headers=h,
                json={
                    "name": "Busy",
                    "dataset_version_id": seeded["dv"],
                    "initial_deposit": 1,
                    "start_date": "2021-01-04",
                },
            )
        )
    finally:
        sem.release()
        limits.reset_heavy_slots()
    assert client.get(f"/api/v1/portfolios/{pid}/valuation", headers=h).status_code == 200


def _ledger(client, seeded, pid: str) -> list:  # type: ignore[no-untyped-def]
    return client.get(f"/api/v1/portfolios/{pid}", headers=seeded["headers"]).json()["transactions"]


def test_a_refused_append_leaves_the_ledger_unchanged(client, seeded, monkeypatch) -> None:
    pid = _create(client, seeded)
    before = _ledger(client, seeded, pid)
    monkeypatch.setattr(get_settings(), "heavy_requests_per_process", 1)
    limits.reset_heavy_slots()
    sem = limits._heavy_semaphore()
    assert sem.acquire(blocking=False)
    try:
        r = client.post(
            f"/api/v1/portfolios/{pid}/transactions",
            headers=seeded["headers"],
            json={"trade_date": "2021-02-01", "kind": "deposit", "amount": 5},
        )
        _assert_busy(r)
    finally:
        sem.release()
        limits.reset_heavy_slots()
    assert _ledger(client, seeded, pid) == before


def test_access_is_checked_before_a_slot_is_taken(client, seeded, monkeypatch) -> None:
    """#223 review: someone who may not see the portfolio gets 404, not 503, even while the
    process is busy, and takes no slot."""
    from tests.integration.conftest import register

    pid = _create(client, seeded)
    outsider = register(client, "outsider@example.com")
    monkeypatch.setattr(get_settings(), "heavy_requests_per_process", 1)
    limits.reset_heavy_slots()
    sem = limits._heavy_semaphore()
    assert sem.acquire(blocking=False)
    try:
        r = client.get(f"/api/v1/portfolios/{pid}/valuation", headers=outsider)
        assert r.status_code == 404, r.text
        r = client.post(
            f"/api/v1/portfolios/{pid}/transactions",
            headers=outsider,
            json={"trade_date": "2021-02-01", "kind": "deposit", "amount": 1},
        )
        assert r.status_code == 404, r.text
        r = client.post(
            f"/api/v1/workspaces/{seeded['ws']}/portfolios",
            headers=outsider,
            json={
                "name": "x",
                "dataset_version_id": seeded["dv"],
                "initial_deposit": 1,
                "start_date": "2021-01-04",
            },
        )
        assert r.status_code == 404, r.text
    finally:
        sem.release()
        limits.reset_heavy_slots()


def test_a_busy_refusal_does_not_spend_the_optimize_budget(client, seeded, monkeypatch) -> None:
    """#223 review: a 503 server_busy did no work, so it is not one of the user's limit."""
    monkeypatch.setattr(get_settings(), "rate_limit_optimize_per_minute", 2)
    monkeypatch.setattr(get_settings(), "heavy_requests_per_process", 1)
    limits.reset_heavy_slots()
    url = f"/api/v1/dataset-versions/{seeded['dv']}/optimize"
    sem = limits._heavy_semaphore()
    assert sem.acquire(blocking=False)
    try:
        refused = [client.post(url, headers=seeded["headers"], json=OPTIMIZE) for _ in range(3)]
    finally:
        sem.release()
        limits.reset_heavy_slots()
    assert [r.status_code for r in refused] == [503, 503, 503]
    codes = [
        client.post(url, headers=seeded["headers"], json=OPTIMIZE).status_code for _ in range(3)
    ]
    assert codes == [200, 200, 429]  # the full budget of 2 is still there


def test_optimize_has_a_per_user_rate_limit(client, seeded, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_optimize_per_minute", 2)
    url = f"/api/v1/dataset-versions/{seeded['dv']}/optimize"
    out = [client.post(url, headers=seeded["headers"], json=OPTIMIZE) for _ in range(3)]
    assert [r.status_code for r in out] == [200, 200, 429]
    # #239: when to try again, in the header and the body; the limiter clock is mid-minute.
    refused = out[2]
    assert refused.headers["retry-after"] == "30"
    assert refused.json()["error"]["details"] == {"retry_after_seconds": 30}
    assert refused.json()["error"]["message"] == "Too many optimizations; try again in 30 s."
