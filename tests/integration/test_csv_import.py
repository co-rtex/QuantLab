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
