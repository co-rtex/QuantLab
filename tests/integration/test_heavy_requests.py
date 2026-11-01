"""The heavy routes refuse rather than stack up (#223): 503 server_busy with Retry-After
when their process has no free slot, and optimize also 429 past its per-user limit."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from quantlab_api import limits
from quantlab_api.config import get_settings
from tests.integration.test_portfolios import _create

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


def test_optimize_has_a_per_user_rate_limit(client, seeded, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_optimize_per_minute", 2)
    url = f"/api/v1/dataset-versions/{seeded['dv']}/optimize"
    codes = [
        client.post(url, headers=seeded["headers"], json=OPTIMIZE).status_code for _ in range(3)
    ]
    assert codes == [200, 200, 429]
