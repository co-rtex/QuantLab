def test_optimizer_compares_methods_point_in_time(client, seeded):
    h, dv = seeded["headers"], seeded["dv"]
    syms = [f"SYN{i:03d}" for i in range(1, 9)]
    r = client.post(
        f"/api/v1/dataset-versions/{dv}/optimize",
        headers=h,
        json={
            "symbols": syms,
            "as_of": "2021-12-31",
            "lookback": 252,
            "max_weight": 0.3,
            "benchmark": "SYNIDX",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["as_of"] <= "2021-12-31"
    by = {x["method"]: x for x in body["results"]}
    for m in ("equal_weight", "min_variance", "mean_variance", "risk_parity"):
        w = by[m]["weights"]
        assert abs(sum(w.values()) - 1) < 1e-6
        assert min(w.values()) >= -1e-9
    assert max(by["min_variance"]["weights"].values()) <= 0.3 + 1e-6
    assert (
        by["min_variance"]["expected_volatility"]
        <= by["equal_weight"]["expected_volatility"] + 1e-9
    )
    shares = list(by["risk_parity"]["risk_share"].values())
    assert max(shares) - min(shares) < 1e-3
    assert by["equal_weight"]["beta"] is not None
    # A later as_of changes the inputs; an earlier one cannot see later data.
    early = client.post(
        f"/api/v1/dataset-versions/{dv}/optimize",
        headers=h,
        json={"symbols": syms, "as_of": "2021-06-30"},
    ).json()
    assert early["as_of"] <= "2021-06-30"
    bad = client.post(
        f"/api/v1/dataset-versions/{dv}/optimize", headers=h, json={"symbols": ["SYN001"]}
    )
    assert bad.status_code == 422
