"""A portfolio's ledger is capped (#218): each append replays the whole ledger under the
portfolio's row lock, so an unbounded ledger made every later append and valuation slower."""

from __future__ import annotations

from sqlalchemy import func, select

from quantlab_api.config import get_settings
from quantlab_api.db import SessionLocal
from quantlab_api.models import PortfolioTransaction
from tests.integration.test_portfolios import _create, tx


def count() -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(PortfolioTransaction)) or 0


def deposit(client, seeded, pid, day):  # type: ignore[no-untyped-def]
    return tx(client, seeded, pid, trade_date=day, kind="deposit", amount=10)


def test_an_append_at_the_cap_is_refused_before_any_work(client, seeded, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "max_portfolio_transactions", 3)
    pid = _create(client, seeded)  # its initial deposit is transaction 1
    assert deposit(client, seeded, pid, "2021-02-01").status_code == 201
    assert deposit(client, seeded, pid, "2021-02-02").status_code == 201
    assert count() == 3

    # Refused whatever the entry, even one naming an unknown symbol: the cap is checked
    # before the market data is loaded or the ledger replayed.
    for body in (
        {"trade_date": "2021-02-03", "kind": "deposit", "amount": 10},
        {"trade_date": "2021-02-03", "kind": "buy", "symbol": "NOPE", "quantity": 1},
    ):
        r = tx(client, seeded, pid, **body)
        assert r.status_code == 422, r.text
        err = r.json()["error"]
        assert err["code"] == "ledger_full"
        assert err["details"] == {"limit": 3, "transactions": 3}
        assert "Start a new portfolio" in err["message"]
    assert count() == 3

    # The full ledger stays readable, and says how close it is to the cap.
    v = client.get(f"/api/v1/portfolios/{pid}/valuation", headers=seeded["headers"])
    assert v.status_code == 200, v.text
    assert v.json()["portfolio"]["transactions"] == 3
    assert v.json()["portfolio"]["max_transactions"] == 3


def test_a_ledger_longer_than_a_lowered_cap_stays_readable(client, seeded, monkeypatch) -> None:
    pid = _create(client, seeded)
    assert deposit(client, seeded, pid, "2021-02-01").status_code == 201
    monkeypatch.setattr(get_settings(), "max_portfolio_transactions", 1)
    v = client.get(f"/api/v1/portfolios/{pid}/valuation", headers=seeded["headers"])
    assert v.status_code == 200, v.text
    assert v.json()["portfolio"]["transactions"] == 2
    r = deposit(client, seeded, pid, "2021-02-02")
    assert r.status_code == 422 and r.json()["error"]["details"]["transactions"] == 2


def test_the_cap_is_per_portfolio(client, seeded, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "max_portfolio_transactions", 2)
    full = _create(client, seeded)
    assert deposit(client, seeded, full, "2021-02-01").status_code == 201
    assert deposit(client, seeded, full, "2021-02-02").status_code == 422
    other = _create(client, seeded, name="Second")
    assert deposit(client, seeded, other, "2021-02-01").status_code == 201


def test_the_default_cap_and_the_valuation_fields(client, seeded) -> None:
    assert get_settings().max_portfolio_transactions == 10_000
    pid = _create(client, seeded)
    body = client.get(f"/api/v1/portfolios/{pid}/valuation", headers=seeded["headers"]).json()
    assert body["portfolio"]["transactions"] == 1
    assert body["portfolio"]["max_transactions"] == 10_000
