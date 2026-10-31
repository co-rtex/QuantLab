"""The licensed-vendor dataset job (#11): inert until configured, and the key never leaks.

No test makes a network call: the vendor is the scripted fake from the unit tests, which
serves *synthetic* data in the vendor's documented response shapes. Versions written here
exist only in the test database.
"""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic import SecretStr

from tests.integration.conftest import run_pending_jobs
from tests.unit.test_vendor_massive import KEY, FakeMassive

WINDOW = {"start": "2024-01-02", "end": "2024-01-31"}


def _submit(seeded, **payload) -> str:
    from sqlalchemy import select

    from quantlab_api.db import SessionLocal
    from quantlab_api.jobs import submit_job
    from quantlab_api.models import Dataset, DatasetVersion, Job

    with SessionLocal() as db:
        ds = db.get(Dataset, db.get(DatasetVersion, uuid.UUID(seeded["dv"])).dataset_id)
        user = db.scalar(select(Job.created_by).where(Job.workspace_id == ds.workspace_id))
        job, _ = submit_job(
            db,
            workspace_id=ds.workspace_id,
            user_id=user,
            kind="dataset.vendor",
            payload={"dataset_id": str(ds.id), **WINDOW, **payload},
            dispatch=False,
        )
        return str(job.id)


def _job(job_id: str):
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Job

    with SessionLocal() as db:
        job = db.get(Job, uuid.UUID(job_id))
        db.expunge(job)
        return job


def _configure(monkeypatch, fake: FakeMassive | None = None, **settings) -> None:
    from quantlab_api import marketdata, tasks
    from quantlab_api.config import get_settings

    configured = get_settings().model_copy(update=settings)
    monkeypatch.setattr(tasks, "get_settings", lambda: configured)
    if fake is not None:
        from quantlab.data.vendors.massive import MassiveProvider

        monkeypatch.setattr(marketdata, "urllib_transport", fake)
        real = MassiveProvider.describe
        # The fake serves synthetic data, so its versions are labelled synthetic.
        monkeypatch.setattr(
            MassiveProvider,
            "describe",
            lambda self: {**real(self), "is_synthetic": True, "fixture": "scripted fake"},
        )


ALL = {
    "market_data_provider": "massive",
    "market_data_api_key": SecretStr(KEY),
    "market_data_license": "test licence (synthetic fixtures)",
    "market_data_plan": "test",
}


def test_the_job_refuses_until_provider_key_licence_and_plan_are_all_set(
    client, seeded, monkeypatch
):
    fake = FakeMassive()
    for missing in (set(ALL), {"market_data_api_key"}, {"market_data_license"}):
        _configure(monkeypatch, fake, **{k: (None if k in missing else v) for k, v in ALL.items()})
        job_id = _submit(seeded)
        assert run_pending_jobs() == ["failed"]
        error = _job(job_id).error["message"]
        assert "not configured" in error
        assert "QUANTLAB_MARKET_DATA_API_KEY" in error or "market_data_api_key" not in missing
    assert fake.requests == []  # inert: nothing was fetched


def test_a_configured_vendor_versions_data_and_the_key_never_leaks(
    client, seeded, monkeypatch, capfd
):
    fake = FakeMassive()
    _configure(monkeypatch, fake, **ALL)
    job_id = _submit(seeded)
    assert run_pending_jobs() == ["completed"]
    job = _job(job_id)
    assert KEY not in json.dumps(job.payload) and KEY not in job.message
    assert fake.requests and all(KEY not in u for u in fake.requests)  # header only
    assert all(h.get("Authorization") == f"Bearer {KEY}" for h in fake.headers)
    h = seeded["headers"]
    versions = client.get(f"/api/v1/workspaces/{seeded['ws']}/dataset-versions", headers=h)
    (listed,) = [v for v in versions.json() if v["id"] != seeded["dv"]]
    assert listed["is_synthetic"] is True  # fixture data stays labelled synthetic
    dv_id = listed["id"]
    version = client.get(f"/api/v1/dataset-versions/{dv_id}", headers=h).json()
    assert version["provenance"]["provider"] == "massive"
    assert version["provenance"]["license"] == ALL["market_data_license"]
    assert KEY not in json.dumps(version)
    inst = client.get(f"/api/v1/dataset-versions/{dv_id}/instruments?limit=50", headers=h).json()
    ids = {i["instrument_id"] for i in inst["items"]}
    assert "BBGSYN000001" in ids  # permanent ids, across the rename
    out, err = capfd.readouterr()
    assert KEY not in out and KEY not in err


def test_a_rejected_key_fails_permanently_without_echoing_it(client, seeded, monkeypatch, capfd):
    fake = FakeMassive()
    fake.fail("/v3/reference/tickers", (401, {}))
    _configure(monkeypatch, fake, **ALL)
    job_id = _submit(seeded)
    assert run_pending_jobs() == ["failed"]
    error = _job(job_id).error
    assert "HTTP 401" in error["message"]
    assert KEY not in json.dumps(error)  # message and traceback
    out, err = capfd.readouterr()
    assert KEY not in out and KEY not in err


@pytest.mark.parametrize("bad", [{"start": "2024-02-01", "end": "2024-01-02"}])
def test_an_inverted_window_is_refused(client, seeded, monkeypatch, bad):
    _configure(monkeypatch, FakeMassive(), **ALL)
    job_id = _submit(seeded, **bad)
    assert run_pending_jobs() == ["failed"]
    assert "start must not be after end" in _job(job_id).error["message"]


# --------------------------------------------------------------- #95 security review
def _catalog(client, seeded) -> dict:
    h = seeded["headers"]
    versions = client.get(f"/api/v1/workspaces/{seeded['ws']}/dataset-versions", headers=h)
    (listed,) = [v for v in versions.json() if v["id"] != seeded["dv"]]
    detail = client.get(f"/api/v1/dataset-versions/{listed['id']}", headers=h).json()
    inst = client.get(
        f"/api/v1/dataset-versions/{listed['id']}/instruments?limit=50", headers=h
    ).json()
    return {"provenance": detail["provenance"], "instruments": inst["items"]}


def test_naming_a_renamed_securitys_old_ticker_keeps_it_alive(client, seeded, monkeypatch):
    # OLDA's reference row is inactive (renamed to NEWA). Filtering rows by ticker kept only
    # that row, so identity() saw no active row and marked the security delisted.
    fake = FakeMassive()
    fake.extra_inactive.append(  # as the vendor lists a renamed security's old ticker
        {
            "ticker": "OLDA",
            "name": "Synthetic Security 1",
            "active": False,
            "delisted_utc": "2024-01-09T21:00:00Z",
            "share_class_figi": "BBGSYN000001",
            "composite_figi": "BBCSYN000001",
            "market": "stocks",
            "type": "CS",
        }
    )
    _configure(monkeypatch, fake, **ALL)
    _submit(seeded, tickers=["olda"])
    assert run_pending_jobs() == ["completed"]
    rows = [i for i in _catalog(client, seeded)["instruments"]]
    assert {r["instrument_id"] for r in rows} == {"BBGSYN000001"}
    assert {r["symbol"] for r in rows} == {"OLDA", "NEWA"}
    assert not any(r["delisted"] for r in rows)


def test_tickers_the_vendor_does_not_know_fail_the_job(client, seeded, monkeypatch):
    fake = FakeMassive()
    _configure(monkeypatch, fake, **ALL)
    job_id = _submit(seeded, tickers=["NEWA", "NOPE1", "NOPE2"])
    assert run_pending_jobs() == ["failed"]
    msg = _job(job_id).error["message"]
    assert "2 requested tickers not in" in msg and "NOPE1" in msg


def test_the_asset_limit_is_checked_before_any_per_security_request(client, seeded, monkeypatch):
    fake = FakeMassive()
    _configure(monkeypatch, fake, **ALL, max_dataset_assets=1)
    job_id = _submit(seeded)
    assert run_pending_jobs() == ["failed"]
    assert "exceed the configured limit" in _job(job_id).error["message"]
    assert fake.requests and all("/events" not in u for u in fake.requests)


def test_provenance_records_full_counts_of_left_out_securities(client, seeded, monkeypatch):
    _configure(monkeypatch, FakeMassive(), **ALL)
    _submit(seeded)
    assert run_pending_jobs() == ["completed"]
    prov = _catalog(client, seeded)["provenance"]
    assert prov["unidentified_count"] == len(prov["unidentified"]) == 1
    assert prov["unidentified"] == ["NOFIGI"]
    assert prov["skipped_no_composite_figi_count"] == len(prov["skipped_no_composite_figi"])
