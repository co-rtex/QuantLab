"""End-to-end API workflow: dataset -> strategy -> async backtest -> results -> reproduce."""

from __future__ import annotations

from tests.integration.conftest import register, run_pending_jobs, workspace_id

BT = {"start": "2021-01-04", "rebalance": "monthly", "benchmark": "SYNIDX",
      "commission_bps": 2, "slippage_bps": 5}


def submit(client, seeded, strategy="momentum", params=None, key=None, name="Momentum test"):
    headers = dict(seeded["headers"])
    if key:
        headers["Idempotency-Key"] = key
    return client.post(
        f"/api/v1/workspaces/{seeded['ws']}/experiments", headers=headers,
        json={"name": name, "dataset_version_id": seeded["dv"], "strategy_key": strategy,
              "strategy_params": params if params is not None else {"lookback": 126, "skip": 21, "top_n": 4},
              "config": BT},
    )


def test_health_ready_openapi(client):
    assert client.get("/api/v1/health").json() == {"status": "ok"}
    r = client.get("/api/v1/ready")
    assert r.status_code == 200, r.text
    spec = client.get("/api/v1/openapi.json").json()
    assert "/api/v1/workspaces/{workspace_id}/experiments" in spec["paths"]
    assert "X-Request-ID" in client.get("/api/v1/health").headers
    assert "quantlab_http_request_duration_seconds" in client.get("/metrics").text


def test_dataset_catalog_and_bars(client, seeded):
    h, dv = seeded["headers"], seeded["dv"]
    d = client.get(f"/api/v1/dataset-versions/{dv}", headers=h).json()
    assert d["is_synthetic"] is True
    assert d["provenance"]["provider"] == "synthetic"
    assert d["quality_status"] == "pass"
    inst = client.get(f"/api/v1/dataset-versions/{dv}/instruments?limit=5", headers=h).json()
    assert inst["total"] == 13  # 12 assets + benchmark
    raw = client.get(f"/api/v1/dataset-versions/{dv}/bars/SYN001?start=2021-01-01", headers=h)
    assert raw.status_code == 200, raw.text
    body = raw.json()
    assert body["is_synthetic"] is True
    assert body["dates"][0] >= "2021-01-01"
    assert body["cache"] == "miss"
    again = client.get(f"/api/v1/dataset-versions/{dv}/bars/SYN001?start=2021-01-01", headers=h)
    assert again.json()["cache"] == "hit"
    adj = client.get(f"/api/v1/dataset-versions/{dv}/bars/SYN001?adjusted=true", headers=h).json()
    assert adj["close"][-1] == body["close"][-1]  # last price is never adjusted
    missing = client.get(f"/api/v1/dataset-versions/{dv}/bars/NOPE", headers=h)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_backtest_workflow_results_and_reproduction(client, seeded):
    h = seeded["headers"]
    r = submit(client, seeded)
    assert r.status_code == 202, r.text
    exp_id = r.json()["experiment"]["id"]
    job_id = r.json()["job"]["id"]
    assert r.json()["job"]["status"] == "queued"
    not_ready = client.get(f"/api/v1/experiments/{exp_id}/equity", headers=h)
    assert not_ready.status_code == 409
    assert run_pending_jobs() == ["completed"]
    job = client.get(f"/api/v1/jobs/{job_id}", headers=h).json()
    assert job["status"] == "completed"
    assert job["progress"] == 1.0
    exp = client.get(f"/api/v1/experiments/{exp_id}", headers=h).json()
    assert exp["status"] == "completed"
    assert exp["summary"]["sharpe"] is not None
    assert len(exp["fingerprint"]) == 64
    eq = client.get(f"/api/v1/experiments/{exp_id}/equity", headers=h).json()
    assert len(eq["dates"]) == len(eq["equity"]) == len(eq["benchmark"])
    assert eq["equity"][0] == 1_000_000
    trades = client.get(f"/api/v1/experiments/{exp_id}/trades?limit=5", headers=h).json()
    assert trades["total"] > 0
    assert len(trades["items"]) == 5
    assert all(t["date"] > t["decision_date"] for t in trades["items"])
    pos = client.get(f"/api/v1/experiments/{exp_id}/positions?on=2022-06-15", headers=h).json()
    assert pos["date"] <= "2022-06-15"
    analysis = client.get(f"/api/v1/experiments/{exp_id}/analysis", headers=h).json()
    assert {"performance", "risk", "drawdowns", "costs", "trades"} <= set(analysis)
    contrib = analysis["contributions"]
    final_equity = eq["equity"][-1]
    assert abs(contrib["total"] - (final_equity - 1_000_000)) < 1e-4 * 1_000_000

    rep = client.post(f"/api/v1/experiments/{exp_id}/reproduce", headers=h)
    assert rep.status_code == 202, rep.text
    child = rep.json()["experiment"]
    assert child["parent_id"] == exp_id
    assert child["input_hash"] == exp["input_hash"]
    assert run_pending_jobs() == ["completed"]
    check = client.get(f"/api/v1/experiments/{child['id']}/reproducibility", headers=h).json()
    assert check["comparisons"][0]["fingerprint_match"] is True
    manifest = client.get(f"/api/v1/experiments/{exp_id}/manifest", headers=h).json()
    assert manifest["dataset"]["is_synthetic"] is True
    assert manifest["result_fingerprint"] == exp["fingerprint"]

    cmp = client.post(f"/api/v1/workspaces/{seeded['ws']}/experiments/compare", headers=h,
                      json={"experiment_ids": [exp_id, child["id"]]}).json()
    assert len(cmp["experiments"]) == 2
    lst = client.get(f"/api/v1/workspaces/{seeded['ws']}/experiments?status=completed",
                     headers=h).json()
    assert lst["total"] == 2


def test_idempotent_submission(client, seeded):
    a = submit(client, seeded, key="abc-1")
    b = submit(client, seeded, key="abc-1")
    assert a.status_code == b.status_code == 202
    assert a.json()["experiment"]["id"] == b.json()["experiment"]["id"]
    assert b.json()["created"] is False
    conflict = submit(client, seeded, key="abc-1", params={"lookback": 63, "skip": 5, "top_n": 3})
    assert conflict.status_code == 409
    lst = client.get(f"/api/v1/workspaces/{seeded['ws']}/experiments",
                     headers=seeded["headers"]).json()
    assert lst["total"] == 1


def test_validation_errors_are_structured(client, seeded):
    bad = submit(client, seeded, params={"lookback": 5, "skip": 10, "top_n": 2})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_strategy_params"
    unknown = submit(client, seeded, strategy="does_not_exist")
    assert unknown.json()["error"]["code"] == "unknown_strategy"
    r = client.post(f"/api/v1/workspaces/{seeded['ws']}/experiments", headers=seeded["headers"],
                    json={"name": "x", "dataset_version_id": seeded["dv"],
                          "strategy_key": "equal_weight",
                          "config": {"universe": ["ZZZ"], "commission_bps": -1}})
    assert r.status_code == 422
    assert r.json()["error"]["request_id"]


def test_saved_strategy_definition(client, seeded):
    h, ws = seeded["headers"], seeded["ws"]
    s = client.post(f"/api/v1/workspaces/{ws}/strategies", headers=h,
                    json={"name": "Trend 20/100", "strategy_key": "sma_crossover",
                          "params": {"fast": 20, "slow": 100}})
    assert s.status_code == 201, s.text
    assert s.json()["params"] == {"fast": 20, "slow": 100}
    r = client.post(f"/api/v1/workspaces/{ws}/experiments", headers=h,
                    json={"name": "trend", "dataset_version_id": seeded["dv"],
                          "strategy_id": s.json()["id"], "config": BT})
    assert r.status_code == 202
    assert r.json()["experiment"]["strategy_key"] == "sma_crossover"
    cat = client.get("/api/v1/strategies/catalog").json()
    assert {c["key"] for c in cat} >= {"momentum", "buy_and_hold", "risk_parity"}


def test_cancel_queued_experiment(client, seeded):
    h = seeded["headers"]
    r = submit(client, seeded).json()
    c = client.post(f"/api/v1/jobs/{r['job']['id']}/cancel", headers=h).json()
    assert c["status"] == "cancelled"
    assert run_pending_jobs() == []
    exp = client.get(f"/api/v1/experiments/{r['experiment']['id']}", headers=h).json()
    assert exp["status"] == "cancelled"
    assert exp["summary"] is None


def test_audit_trail(client, seeded):
    submit(client, seeded)
    a = client.get(f"/api/v1/workspaces/{seeded['ws']}/audit", headers=seeded["headers"]).json()
    actions = {e["action"] for e in a["items"]}
    assert {"user.register", "dataset.ingest_requested", "experiment.submit"} <= actions


def test_second_user_workspace_isolation(client, seeded):
    other = register(client, "mallory@example.com")
    ws, dv = seeded["ws"], seeded["dv"]
    assert client.get(f"/api/v1/workspaces/{ws}", headers=other).status_code == 404
    assert client.get(f"/api/v1/dataset-versions/{dv}", headers=other).status_code == 404
    assert client.get(f"/api/v1/workspaces/{ws}/experiments", headers=other).status_code == 404
    own = workspace_id(client, other)
    r = client.post(f"/api/v1/workspaces/{own}/experiments", headers=other,
                    json={"name": "steal", "dataset_version_id": dv,
                          "strategy_key": "equal_weight", "config": {}})
    assert r.status_code == 404
