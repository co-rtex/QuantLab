from tests.integration.conftest import register, run_pending_jobs


def test_ml_run_end_to_end(client, seeded):
    h, ws = seeded["headers"], seeded["ws"]
    r = client.post(
        f"/api/v1/workspaces/{ws}/ml-runs",
        headers={**h, "Idempotency-Key": "ml-1"},
        json={
            "name": "Direction baseline",
            "dataset_version_id": seeded["dv"],
            "config": {
                "folds": 2,
                "min_train_dates": 6,
                "sample_every": 10,
                "models": ["majority", "momentum_rule", "logistic"],
            },
        },
    )
    assert r.status_code == 202, r.text
    again = client.post(
        f"/api/v1/workspaces/{ws}/ml-runs",
        headers={**h, "Idempotency-Key": "ml-1"},
        json={
            "name": "Direction baseline",
            "dataset_version_id": seeded["dv"],
            "config": {
                "folds": 2,
                "min_train_dates": 6,
                "sample_every": 10,
                "models": ["majority", "momentum_rule", "logistic"],
            },
        },
    )
    assert again.json()["id"] == r.json()["id"]
    assert run_pending_jobs() == ["completed"]
    run = client.get(f"/api/v1/ml-runs/{r.json()['id']}", headers=h).json()
    assert run["status"] == "completed"
    res = run["result"]
    assert {s["model"] for s in res["summary"]} == {"majority", "momentum_rule", "logistic"}
    assert len(res["panel"]["hash"]) == 64
    assert "predictions" not in res
    preds = client.get(
        f"/api/v1/ml-runs/{run['id']}/predictions?model=logistic&limit=5", headers=h
    ).json()
    assert len(preds["items"]) == 5
    assert {"date", "symbol", "prob", "label", "fwd_return"} <= set(preds["items"][0])
    listed = client.get(f"/api/v1/workspaces/{ws}/ml-runs", headers=h).json()
    assert listed[0]["summary"]
    other = register(client, "nosy@example.com")
    assert client.get(f"/api/v1/ml-runs/{run['id']}", headers=other).status_code == 404


def test_ml_run_validation(client, seeded):
    h, ws = seeded["headers"], seeded["ws"]
    bad = client.post(
        f"/api/v1/workspaces/{ws}/ml-runs",
        headers=h,
        json={"name": "x", "dataset_version_id": seeded["dv"], "config": {"models": ["deep_net"]}},
    )
    assert bad.status_code == 422
    bad2 = client.post(
        f"/api/v1/workspaces/{ws}/ml-runs",
        headers=h,
        json={"name": "x", "dataset_version_id": seeded["dv"], "config": {"horizon": 1000}},
    )
    assert bad2.json()["error"]["code"] == "invalid_config"


def test_the_pipeline_version_enters_the_ml_input_hash(client, seeded, monkeypatch):
    """#120: a new pipeline gives a different panel for the same dataset and config, so
    old and new runs must not share an input hash."""
    import hashlib
    import json

    from quantlab_api.routes import ml as ml_routes

    h, ws = seeded["headers"], seeded["ws"]
    body = {
        "name": "hash",
        "dataset_version_id": seeded["dv"],
        "config": {"folds": 2, "min_train_dates": 6, "sample_every": 10, "models": ["majority"]},
    }
    now = client.post(f"/api/v1/workspaces/{ws}/ml-runs", headers=h, json=body)
    assert now.status_code == 202, now.text
    run = client.get(f"/api/v1/ml-runs/{now.json()['id']}", headers=h).json()
    dv = client.get(f"/api/v1/dataset-versions/{seeded['dv']}", headers=h).json()
    expected = hashlib.sha256(
        json.dumps(
            {
                "dataset": dv["version_hash"],
                "config": run["config"],
                "pipeline": ml_routes.ML_PIPELINE_VERSION,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    assert run["input_hash"] == expected
    monkeypatch.setattr(ml_routes, "ML_PIPELINE_VERSION", ml_routes.ML_PIPELINE_VERSION - 1)
    old = client.post(f"/api/v1/workspaces/{ws}/ml-runs", headers=h, json=body)
    assert old.status_code == 202, old.text
    old_run = client.get(f"/api/v1/ml-runs/{old.json()['id']}", headers=h).json()
    assert old_run["input_hash"] != run["input_hash"]  # same dataset and config, other pipeline
