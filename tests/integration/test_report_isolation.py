"""Reconciliation reports are private to the workspace holding the version (#96).

Two workspaces with the same slug and content share one version directory. A report
written for one of them must never be served to the other. Synthetic data only.
"""

from __future__ import annotations

import uuid

from tests.integration.conftest import register, run_pending_jobs, workspace_id

DEMO = {
    "slug": "demo",
    "name": "Demo",
    "n_assets": 12,
    "start": "2020-01-02",
    "end": "2022-12-30",
    "seed": 5,
}  # the seeded fixture's dataset: same slug and content, so the same directory


def _store(dv_id: str):
    from quantlab.data.store import DatasetStore
    from quantlab_api.config import get_settings
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Dataset, DatasetVersion

    with SessionLocal() as db:
        row = db.get(DatasetVersion, uuid.UUID(dv_id))
        ds = db.get(Dataset, row.dataset_id)
    store = DatasetStore(get_settings().datasets_dir)
    return store, store.open(ds.slug, row.version_hash)


def _reconciliations(client, h, ws: str, dv: str) -> str:
    return f"/api/v1/workspaces/{ws}/dataset-versions/{dv}/reconciliations"


def test_another_workspace_with_the_same_version_cannot_read_a_report(client, seeded):
    a_h, a_ws, a_dv = seeded["headers"], seeded["ws"], seeded["dv"]
    b_h = register(client, email="bob@example.com")
    b_ws = workspace_id(client, b_h)
    r = client.post(f"/api/v1/workspaces/{b_ws}/datasets/synthetic", headers=b_h, json=DEMO)
    assert r.status_code == 202, r.text
    assert run_pending_jobs() == ["completed"]
    (b_dv,) = [
        v["id"]
        for v in client.get(f"/api/v1/workspaces/{b_ws}/dataset-versions", headers=b_h).json()
    ]
    store, version = _store(a_dv)
    _, b_version = _store(b_dv)
    assert version.path == b_version.path  # one shared content directory

    from quantlab.data.reconcile import Source, reconcile

    primary = Source.from_version(store, version, "primary", is_synthetic=True)
    report = reconcile(primary, Source("vendor-x", primary.bars, primary.actions, None, True))
    store.write_report(version, "reconciliation-private", report.to_dict(), owner=a_dv)
    a_base, b_base = (
        _reconciliations(client, a_h, a_ws, a_dv),
        _reconciliations(client, b_h, b_ws, b_dv),
    )
    assert [x["name"] for x in client.get(a_base, headers=a_h).json()] == ["reconciliation-private"]
    assert client.get(f"{a_base}/reconciliation-private", headers=a_h).status_code == 200
    # B sees nothing, by listing or by name, through its own version of the same content.
    assert client.get(b_base, headers=b_h).json() == []
    assert client.get(f"{b_base}/reconciliation-private", headers=b_h).status_code == 404
    # Nor through A's version id, which is outside B's workspace.
    assert (
        client.get(
            f"/api/v1/workspaces/{b_ws}/dataset-versions/{a_dv}/reconciliations", headers=b_h
        ).status_code
        == 404
    )


def test_reports_stored_beside_the_shared_directory_are_not_served(client, seeded):
    """Before #96 reports lived at <slug>/_reports/<hash>/, readable by every holder."""
    import json

    h, ws, dv = seeded["headers"], seeded["ws"], seeded["dv"]
    store, version = _store(dv)
    legacy = store.root / version.slug / "_reports" / version.version
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "reconciliation-legacy.json").write_text(json.dumps({"status": "pass"}))
    base = _reconciliations(client, h, ws, dv)
    assert client.get(base, headers=h).json() == []
    assert client.get(f"{base}/reconciliation-legacy", headers=h).status_code == 404


def test_deleting_a_workspace_removes_its_reports_only(client, seeded):
    a_h, a_ws, a_dv = seeded["headers"], seeded["ws"], seeded["dv"]
    store, version = _store(a_dv)
    path = store.write_report(version, "reconciliation-a", {"status": "pass"}, owner=a_dv)
    other = store.write_report(
        version, "reconciliation-b", {"status": "pass"}, owner="someone-else"
    )
    assert client.delete(f"/api/v1/workspaces/{a_ws}", headers=a_h).status_code == 204
    assert not path.exists() and other.exists()
