from tests.integration.conftest import register


def _create(client, seeded, **kw):
    body = {
        "name": "Core",
        "dataset_version_id": seeded["dv"],
        "benchmark": "SYNIDX",
        "initial_deposit": 100_000,
        "start_date": "2021-01-04",
        **kw,
    }
    r = client.post(
        f"/api/v1/workspaces/{seeded['ws']}/portfolios", headers=seeded["headers"], json=body
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def tx(client, seeded, pid, **body):
    return client.post(
        f"/api/v1/portfolios/{pid}/transactions", headers=seeded["headers"], json=body
    )


def test_portfolio_ledger_and_valuation(client, seeded):
    h = seeded["headers"]
    pid = _create(client, seeded)
    buy = tx(
        client,
        seeded,
        pid,
        trade_date="2021-01-05",
        kind="buy",
        symbol="syn001",
        quantity=100,
        commission=1,
    )
    assert buy.status_code == 201, buy.text
    assert buy.json()["price"] > 0  # defaulted to the session close
    assert (
        tx(
            client, seeded, pid, trade_date="2021-02-01", kind="buy", symbol="SYN002", quantity=50
        ).status_code
        == 201
    )
    assert (
        tx(client, seeded, pid, trade_date="2021-06-01", kind="deposit", amount=10_000).status_code
        == 201
    )
    v = client.get(f"/api/v1/portfolios/{pid}/valuation", headers=h).json()
    assert v["portfolio"]["is_synthetic"] is True
    hold = {x["symbol"]: x for x in v["holdings"]}
    assert set(hold) == {"SYN001", "SYN002"}
    s = v["summary"]
    assert abs(s["equity"] - (s["cash"] + sum(x["value"] for x in v["holdings"]))) < 1e-6
    assert s["net_contributions"] == 110_000
    assert v["series"]["index"][0] == 100.0
    assert len(v["series"]["benchmark"]) == len(v["series"]["dates"])
    assert v["risk"]["concentration"]["effective_n"] > 1
    assert v["risk"]["stress"][0]["portfolio_return"] < 0
    assert "terminal_quantiles" in v["risk"]["monte_carlo"]
    assert v["performance"]["values"]["max_drawdown"] <= 0


def test_impossible_transactions_are_rejected_and_not_stored(client, seeded):
    pid = _create(client, seeded, initial_deposit=1_000)
    over = tx(
        client, seeded, pid, trade_date="2021-01-05", kind="buy", symbol="SYN001", quantity=10_000
    )
    assert over.status_code == 422
    assert "transaction #2" in over.json()["error"]["message"]
    short = tx(
        client, seeded, pid, trade_date="2021-01-05", kind="sell", symbol="SYN001", quantity=1
    )
    assert short.status_code == 422
    weekend = tx(client, seeded, pid, trade_date="2021-01-09", kind="deposit", amount=5)
    assert weekend.status_code == 422
    unknown = tx(
        client, seeded, pid, trade_date="2021-01-05", kind="buy", symbol="NOPE", quantity=1
    )
    assert unknown.status_code == 422
    p = client.get(f"/api/v1/portfolios/{pid}", headers=seeded["headers"]).json()
    assert len(p["transactions"]) == 1  # only the initial deposit survived


def test_portfolio_isolation(client, seeded):
    pid = _create(client, seeded)
    other = register(client, "eve@example.com")
    assert client.get(f"/api/v1/portfolios/{pid}", headers=other).status_code == 404
    r = client.post(
        f"/api/v1/portfolios/{pid}/transactions",
        headers=other,
        json={"trade_date": "2021-01-05", "kind": "deposit", "amount": 1},
    )
    assert r.status_code == 404
