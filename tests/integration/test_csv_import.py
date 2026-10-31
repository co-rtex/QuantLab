import io

from tests.integration.conftest import register, run_pending_jobs, workspace_id

BARS = """symbol,date,open,high,low,close,volume
abc,2024-01-02,10,10.5,9.8,10.2,1000
abc,2024-01-03,10.2,10.4,10.0,10.1,1200
abc,2024-01-04,10.1,10.3,9.9,10.0,900
xyz,2024-01-02,50,51,49,50.5,500
xyz,2024-01-03,50.5,52,50,51.5,700
xyz,2024-01-04,51.5,52,25,25.8,800
"""
ACTIONS = "symbol,ex_date,kind,value\nxyz,2024-01-04,split,2\n"


def _upload(client, h, ws, bars=BARS, actions=ACTIONS, slug="my-data"):
    files = {"bars": ("bars.csv", io.BytesIO(bars.encode()), "text/csv")}
    if actions is not None:
        files["actions"] = ("actions.csv", io.BytesIO(actions.encode()), "text/csv")
    return client.post(
        f"/api/v1/workspaces/{ws}/datasets/csv",
        headers=h,
        files=files,
        data={
            "slug": slug,
            "name": "My data",
            "source": "Broker export",
            "license": "Personal research use",
        },
    )


def test_csv_import_creates_versioned_dataset(client):
    h = register(client)
    ws = workspace_id(client, h)
    r = _upload(client, h, ws)
    assert r.status_code == 202, r.text
    assert run_pending_jobs() == ["completed"]
    dv = client.get(f"/api/v1/workspaces/{ws}/dataset-versions", headers=h).json()[0]
    assert dv["is_synthetic"] is False
    detail = client.get(f"/api/v1/dataset-versions/{dv['id']}", headers=h).json()
    assert detail["provenance"]["source"] == "Broker export"
    assert detail["provenance"]["license"] == "Personal research use"
    adj = client.get(
        f"/api/v1/dataset-versions/{dv['id']}/bars/XYZ?adjusted=true", headers=h
    ).json()
    assert abs(adj["close"][0] - 50.5 / 2) < 1e-6  # split applied to earlier prices


def test_csv_import_rejects_bad_input(client):
    h = register(client)
    ws = workspace_id(client, h)
    r = _upload(client, h, ws, bars="symbol,date,close\nabc,2024-01-02,1\n", actions=None)
    assert r.status_code == 202
    assert run_pending_jobs() == ["failed"]
    job = client.get(f"/api/v1/workspaces/{ws}/jobs", headers=h).json()["items"][0]
    assert "invalid CSV" in job["error"]["message"]
    evil = _upload(client, h, ws, bars=BARS.replace("abc", "a b;drop"), slug="evil")
    assert evil.status_code == 202
    assert run_pending_jobs() == ["failed"]
    missing_license = client.post(
        f"/api/v1/workspaces/{ws}/datasets/csv",
        headers=h,
        files={"bars": ("b.csv", io.BytesIO(BARS.encode()), "text/csv")},
        data={"slug": "x1", "name": "x", "source": "somewhere"},
    )
    assert missing_license.status_code == 422


def test_failed_import_releases_staged_files_and_body_limits(client):
    from quantlab_api.config import get_settings

    h = register(client, "lim@example.com")
    ws = workspace_id(client, h)
    _upload(client, h, ws, bars="symbol,date,close\nabc,2024-01-02,1\n", actions=None, slug="bad")
    assert run_pending_jobs() == ["failed"]
    uploads = get_settings().data_dir / "uploads"
    assert not any(uploads.iterdir())
    big = client.post(
        f"/api/v1/workspaces/{ws}/strategies",
        headers={**h, "content-type": "application/json"},
        content=b"{" + b" " * (3 * 2**20) + b"}",
    )
    assert big.status_code == 413
    assert big.json()["error"]["code"] == "payload_too_large"


def test_rejected_uploads_do_not_leave_staged_files(client, monkeypatch):
    from quantlab_api.config import get_settings

    monkeypatch.setattr(get_settings(), "max_active_jobs_per_workspace", 1)
    h = register(client)
    ws = workspace_id(client, h)
    uploads = get_settings().data_dir / "uploads"
    before = set(uploads.iterdir()) if uploads.exists() else set()
    codes = [_upload(client, h, ws, slug=f"cap-{i}").status_code for i in range(4)]
    assert codes == [202, 429, 429, 429]
    assert len(set(uploads.iterdir()) - before) == 1  # only the accepted job's files remain


def test_malformed_corporate_actions_block_ingestion_with_a_report(client):
    """Ingestion gates (#10): xyz's 2-for-1 split recorded as 4-for-1, plus a duplicated
    row of a dividend. Nothing is registered and the job error names each failing gate.
    An action for a symbol without bars only warns (the engine never applies it)."""
    from quantlab_api.config import get_settings

    h = register(client, "gate@example.com")
    ws = workspace_id(client, h)
    uploads = get_settings().data_dir / "uploads"
    before = set(uploads.iterdir()) if uploads.exists() else set()
    bad = (
        "symbol,ex_date,kind,value\nxyz,2024-01-04,split,4\n"
        "abc,2024-01-03,dividend,0.05\nabc,2024-01-03,dividend,0.05\n"
        "nobars,2024-01-03,dividend,0.5\n"
    )
    r = _upload(client, h, ws, actions=bad, slug="gated")
    assert r.status_code == 202, r.text
    assert run_pending_jobs() == ["failed"]
    job = client.get(f"/api/v1/workspaces/{ws}/jobs", headers=h).json()["items"][0]
    message = job["error"]["message"]
    assert "ingestion gate" in message
    assert "split_price_mismatch" in message and "duplicate_corporate_actions" in message
    assert "orphan_corporate_actions" not in message
    versions = client.get(f"/api/v1/workspaces/{ws}/dataset-versions", headers=h).json()
    assert versions == []
    assert set(uploads.iterdir()) == before  # the staged upload was released
    # The same data with the correct ratio is accepted.
    ok = _upload(client, h, ws, slug="gated")
    assert ok.status_code == 202 and run_pending_jobs() == ["completed"]
