"""Every experiment, ML-run and portfolio row says whether its data is synthetic (#102).

The flag comes from the referenced dataset version. A row whose version is missing says
``null`` ("unknown"), never ``false``, so the web never presents it as real data.
"""

from __future__ import annotations

import uuid

import pytest

from tests.integration.conftest import run_pending_jobs
from tests.integration.test_csv_import import _upload


def _csv_version(client, seeded) -> str:
    """A user-supplied (non-synthetic) version: the CSV import tests' fixture."""
    h, ws = seeded["headers"], seeded["ws"]
    assert _upload(client, h, ws, slug="broker").status_code == 202
    assert run_pending_jobs() == ["completed"]
    versions = client.get(f"/api/v1/workspaces/{ws}/dataset-versions", headers=h).json()
    (real,) = [v["id"] for v in versions if not v["is_synthetic"]]
    return real


def _make(client, seeded, kind: str, dv: str) -> str:
    h, ws = seeded["headers"], seeded["ws"]
    if kind == "experiments":
        body = {
            "name": "flag",
            "dataset_version_id": dv,
            "strategy_key": "equal_weight",
            "strategy_params": {},
            "config": {},
        }
        r = client.post(f"/api/v1/workspaces/{ws}/experiments", headers=h, json=body)
        assert r.status_code == 202, r.text
        return r.json()["experiment"]["id"]
    if kind == "ml-runs":
        body = {"name": "flag", "dataset_version_id": dv, "config": {}}
        r = client.post(f"/api/v1/workspaces/{ws}/ml-runs", headers=h, json=body)
        assert r.status_code == 202, r.text
        return r.json()["id"]
    start = client.get(f"/api/v1/dataset-versions/{dv}", headers=h).json()["start_date"]
    body = {"name": "flag", "dataset_version_id": dv, "initial_deposit": 1000, "start_date": start}
    r = client.post(f"/api/v1/workspaces/{ws}/portfolios", headers=h, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _rows(client, seeded, kind: str) -> dict[str, dict]:
    h, ws = seeded["headers"], seeded["ws"]
    body = client.get(f"/api/v1/workspaces/{ws}/{kind}", headers=h).json()
    items = body["items"] if isinstance(body, dict) else body
    return {i["id"]: i for i in items}


def _detail(client, seeded, kind: str, row_id: str) -> dict:
    return client.get(f"/api/v1/{kind}/{row_id}", headers=seeded["headers"]).json()


def _drop_version(dv: str) -> None:
    """Make a referenced version vanish, as only a damaged database could.

    The foreign key forbids it, so its triggers are skipped for this one delete.
    """
    from sqlalchemy import text

    from quantlab_api.db import SessionLocal

    with SessionLocal() as db:
        db.execute(text("SET LOCAL session_replication_role = replica"))
        db.execute(text("DELETE FROM instruments WHERE dataset_version_id = :v"), {"v": dv})
        db.execute(text("DELETE FROM dataset_versions WHERE id = :v"), {"v": dv})
        db.commit()


@pytest.mark.parametrize("kind", ["experiments", "ml-runs", "portfolios"])
def test_rows_carry_their_versions_synthetic_flag(client, seeded, kind):
    real_dv = _csv_version(client, seeded)  # before any row: it runs the queued jobs
    synthetic = _make(client, seeded, kind, seeded["dv"])
    real = _make(client, seeded, kind, real_dv)
    rows = _rows(client, seeded, kind)
    assert rows[synthetic]["is_synthetic"] is True
    assert rows[real]["is_synthetic"] is False
    assert _detail(client, seeded, kind, synthetic)["is_synthetic"] is True
    assert _detail(client, seeded, kind, real)["is_synthetic"] is False


@pytest.mark.parametrize("kind", ["experiments", "ml-runs", "portfolios"])
def test_a_row_whose_version_is_missing_is_unknown_not_real(client, seeded, kind):
    real = _csv_version(client, seeded)
    row = _make(client, seeded, kind, real)
    _drop_version(real)
    listed = _rows(client, seeded, kind)[row]
    assert "is_synthetic" in listed and listed["is_synthetic"] is None
    detail = _detail(client, seeded, kind, row)
    assert detail["is_synthetic"] is None


def test_the_flag_is_additive(client, seeded):
    """Existing fields are all still there."""
    exp = _make(client, seeded, "experiments", seeded["dv"])
    row = _rows(client, seeded, "experiments")[exp]
    assert {"id", "name", "dataset_version_id", "status", "input_hash"} <= set(row)
    assert uuid.UUID(row["dataset_version_id"])
