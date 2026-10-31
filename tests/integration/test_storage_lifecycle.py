"""Workspace deletion removes its files, never a shared one (#21). Synthetic data only."""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

from tests.integration.conftest import register, run_pending_jobs, workspace_id

DEMO = {
    "slug": "demo",
    "name": "Demo",
    "n_assets": 12,
    "start": "2020-01-02",
    "end": "2022-12-30",
    "seed": 5,
}  # the seeded fixture's dataset: same content, so the same version directory


def _settings():
    from quantlab_api.config import get_settings

    return get_settings()


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path, monkeypatch):
    """A data directory for this test alone. The session's shared one holds files of
    earlier tests whose rows were truncated, which the sweep rightly refuses to touch."""
    monkeypatch.setattr(_settings(), "data_dir", tmp_path / "data")


def _version(client, h, dv_id: str) -> dict:
    return client.get(f"/api/v1/dataset-versions/{dv_id}", headers=h).json()


def _synthetic(client, h, ws: str, **over) -> str:
    r = client.post(f"/api/v1/workspaces/{ws}/datasets/synthetic", headers=h, json={**DEMO, **over})
    assert r.status_code == 202, r.text
    assert run_pending_jobs() == ["completed"]
    versions = client.get(f"/api/v1/workspaces/{ws}/dataset-versions", headers=h).json()
    slug = over.get("slug", DEMO["slug"])
    datasets = {
        d["id"]: d["slug"]
        for d in client.get(f"/api/v1/workspaces/{ws}/datasets", headers=h).json()
    }
    (v,) = [v for v in versions if datasets[v["dataset_id"]] == slug]
    return v["id"]


def _vdir(slug: str, version_hash: str):
    return _settings().datasets_dir / slug / version_hash


def _experiment(client, h, ws: str, dv: str) -> str:
    r = client.post(
        f"/api/v1/workspaces/{ws}/experiments",
        headers=h,
        json={
            "name": "lifecycle",
            "dataset_version_id": dv,
            "strategy_key": "equal_weight",
            "strategy_params": {},
            "config": {"start": "2021-01-04"},
        },
    )
    assert r.status_code == 202, r.text
    assert run_pending_jobs() == ["completed"]
    return r.json()["experiment"]["id"]


def _two_workspaces_sharing_a_version(client, seeded):
    a_h, a_ws, a_dv = seeded["headers"], seeded["ws"], seeded["dv"]
    b_h = register(client, email="bob@example.com")
    b_ws = workspace_id(client, b_h)
    b_dv = _synthetic(client, b_h, b_ws)
    va, vb = _version(client, a_h, a_dv), _version(client, b_h, b_dv)
    assert va["version_hash"] == vb["version_hash"]  # one shared directory
    own = _synthetic(client, a_h, a_ws, slug="mine", seed=6)  # A's alone
    return {
        "a": (a_h, a_ws, a_dv),
        "b": (b_h, b_ws, b_dv),
        "shared": _vdir("demo", va["version_hash"]),
        "own": _vdir("mine", _version(client, a_h, own)["version_hash"]),
        "experiment": _settings().artifacts_dir
        / "experiments"
        / _experiment(client, a_h, a_ws, a_dv),
    }


def test_deleting_a_workspace_removes_its_files_but_not_a_shared_version(client, seeded):
    s = _two_workspaces_sharing_a_version(client, seeded)
    a_h, a_ws, _ = s["a"]
    b_h, _, b_dv = s["b"]
    assert s["shared"].exists() and s["own"].exists() and s["experiment"].exists()
    assert client.delete(f"/api/v1/workspaces/{a_ws}", headers=a_h).status_code == 204
    assert not s["own"].exists() and not s["own"].parent.exists()  # no orphaned files
    assert not s["experiment"].exists()
    assert s["shared"].exists()  # still referenced by B
    bars = client.get(f"/api/v1/dataset-versions/{b_dv}/bars/SYN001", headers=b_h)
    assert bars.status_code == 200 and bars.json()["dates"]
    _, b_ws, _ = s["b"]
    assert client.delete(f"/api/v1/workspaces/{b_ws}", headers=b_h).status_code == 204
    assert not s["shared"].exists()  # the last reference is gone
    reports = s["shared"].parent / "_reports" / s["shared"].name
    assert not reports.exists()


def test_a_crash_after_the_commit_leaves_only_garbage_the_sweep_collects(
    client, seeded, monkeypatch
):
    from quantlab_api import storage

    s = _two_workspaces_sharing_a_version(client, seeded)
    a_h, a_ws, _ = s["a"]
    b_h, b_ws, b_dv = s["b"]
    kept = _settings().artifacts_dir / "experiments" / _experiment(client, b_h, b_ws, b_dv)
    monkeypatch.setattr(storage, "collect", lambda g: {})  # "crash" before any deletion
    assert client.delete(f"/api/v1/workspaces/{a_ws}", headers=a_h).status_code == 204
    assert s["own"].exists() and s["experiment"].exists()  # garbage, not dangling rows
    assert storage.sweep_orphans()["versions"] == 0  # younger than the grace period
    assert s["own"].exists()
    swept = storage.sweep_orphans(min_age_seconds=0, throttle=False)
    assert swept["versions"] == 1 and swept["experiments"] == 1
    assert not s["own"].exists() and not s["experiment"].exists()
    assert s["shared"].exists() and kept.exists()  # never anything still referenced
    trash = _settings().data_dir / ".trash"
    assert any(trash.rglob("manifest.json"))  # quarantined, not yet deleted
    assert storage.purge_trash(retention_seconds=0) >= 1
    assert not any(trash.rglob("manifest.json"))
    assert (
        client.get(f"/api/v1/dataset-versions/{b_dv}/bars/SYN001", headers=b_h).status_code == 200
    )


def test_the_grace_period_bounds_every_write_and_young_temporaries_are_kept(client, seeded):
    from quantlab_api import storage
    from quantlab_api.db import SessionLocal

    with SessionLocal() as db:
        grace = storage.grace_seconds(db)
    assert grace >= storage.MIN_GRACE_SECONDS
    assert grace >= 2 * _settings().job_default_timeout_seconds
    root = _settings().datasets_dir / "demo"
    young, old = root / ".tmp-young", root / ".tmp-old"
    young.mkdir()
    old.mkdir()
    two_days_ago = time.time() - 2 * 86400
    os.utime(old, (two_days_ago, two_days_ago))
    reports_tmp = root / "_reports" / ("0" * 64) / ".tmp-report"
    reports_tmp.parent.mkdir(parents=True)
    reports_tmp.write_text("{")
    os.utime(reports_tmp, (two_days_ago, two_days_ago))
    leftover = _settings().artifacts_dir / "experiments" / f"{uuid.uuid4()}.old-1a2b3c4d"
    leftover.mkdir(parents=True)
    os.utime(leftover, (two_days_ago, two_days_ago))
    storage.sweep_orphans(throttle=False)
    assert young.exists() and not old.exists()
    assert not reports_tmp.exists() and not leftover.exists()
    young.rmdir()


# ------------------------------------------------- #97 review: never a mass deletion
def _old_everything(root) -> None:
    """As restored files keep their original mtimes."""
    week_ago = time.time() - 7 * 86400
    for p in [root, *root.rglob("*")]:
        os.utime(p, (week_ago, week_ago))


def test_a_restore_in_progress_deletes_nothing(client, seeded):
    """pg_restore -c drops and reloads the tables: for a while the database has no rows
    while the disk is full of old files. The sweep must refuse, not empty the store."""
    from sqlalchemy import text

    from quantlab_api import storage
    from quantlab_api.db import SessionLocal
    from tests.integration.conftest import TABLES

    h, ws, dv = seeded["headers"], seeded["ws"], seeded["dv"]
    exp_dir = _settings().artifacts_dir / "experiments" / _experiment(client, h, ws, dv)
    version_dir = _vdir("demo", _version(client, h, dv)["version_hash"])
    _old_everything(_settings().data_dir)
    with SessionLocal() as db:
        db.execute(text("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"))
        db.commit()
    swept = storage.sweep_orphans(throttle=False)
    assert swept["versions"] == 0 and swept["experiments"] == 0
    assert version_dir.exists() and exp_dir.exists()


def test_a_cycle_that_would_remove_most_of_the_store_is_refused(client, seeded):
    import shutil

    from quantlab_api import storage

    h, dv = seeded["headers"], seeded["dv"]
    real = _vdir("demo", _version(client, h, dv)["version_hash"])
    fakes = [real.parent / f"{k:064x}" for k in range(1, 5)]
    for f in fakes:  # four unreferenced copies against one referenced version
        shutil.copytree(real, f)
    _old_everything(_settings().data_dir)
    assert storage.sweep_orphans(throttle=False)["versions"] == 0
    assert all(f.exists() for f in fakes) and real.exists()
    forced = storage.sweep_orphans(min_age_seconds=0, force=True)
    assert forced["versions"] == 4 and real.exists()


def test_the_sweep_can_be_disabled_and_is_throttled(client, seeded, monkeypatch):
    from quantlab_api import storage

    monkeypatch.setattr(_settings(), "storage_sweep", False)
    assert storage.sweep_orphans().get("skipped") is True
    monkeypatch.setattr(_settings(), "storage_sweep", True)
    assert "skipped" not in storage.sweep_orphans()  # first run in this data directory
    assert storage.sweep_orphans().get("skipped") is True  # within the interval


def test_concurrent_renames_do_not_abort_a_cycle(client, seeded, monkeypatch):
    from pathlib import Path

    from quantlab.data.store import DatasetStore

    store = DatasetStore(_settings().datasets_dir)
    real_iterdir = Path.iterdir

    def vanishing(self):
        if self.parent == store.root:  # a slug directory renamed away mid-walk
            raise FileNotFoundError(self)
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", vanishing)
    assert list(store.versions_on_disk()) == []
    assert store.stale_temporaries(0) == []


def test_collect_never_raises_after_the_commit(monkeypatch):
    from quantlab_api import storage

    def broken():
        raise OSError(5, "I/O error")

    monkeypatch.setattr(storage, "get_settings", broken)
    assert storage.collect(storage.Garbage(experiments={str(uuid.uuid4())}))["versions"] == 0
    assert not storage._inside(_settings().data_dir / "uploads", _settings().data_dir / "uploads")


def _race_once(i: int, owner_ws: uuid.UUID, other_ws: uuid.UUID, user: uuid.UUID) -> None:
    """GC of A's last reference to V racing B's registration of the same V."""
    from quantlab.data.store import DatasetStore
    from quantlab.data.synthetic import SyntheticSpec, generate_market
    from quantlab_api import storage
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Dataset, DatasetVersion
    from quantlab_api.tasks import gate_quality, register_version

    m = generate_market(
        SyntheticSpec(n_assets=3, start="2021-01-04", end="2021-03-31", seed=100 + i)
    )
    q = gate_quality(m.bars, m.actions)
    slug = f"race{i}"
    with SessionLocal() as db:
        da = Dataset(
            workspace_id=owner_ws, slug=slug, name=slug, provider="synthetic", created_by=user
        )
        db_ = Dataset(
            workspace_id=other_ws, slug=slug, name=slug, provider="synthetic", created_by=user
        )
        db.add_all([da, db_])
        db.flush()
        dv = register_version(db, da, m.bars, m.actions, m.provenance, None, quality=q)
        db.commit()
        version, a_id, b_id = dv.version_hash, dv.id, db_.id
    store = DatasetStore(_settings().datasets_dir)
    slow = storage._quarantine

    def slow_remove(*a, **k):
        time.sleep(0.05)  # widen the window between the reference check and the delete
        return slow(*a, **k)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def gc() -> None:
        try:
            with SessionLocal() as db:
                db.delete(db.get(DatasetVersion, a_id))
                db.commit()
            barrier.wait()
            storage.collect(storage.Garbage(versions={(slug, version)}))
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    def writer() -> None:
        try:
            barrier.wait()
            with SessionLocal() as db:
                register_version(
                    db, db.get(Dataset, b_id), m.bars, m.actions, m.provenance, None, quality=q
                )
                db.commit()
        except BaseException as exc:
            errors.append(exc)

    storage._quarantine = slow_remove  # type: ignore[assignment]
    try:
        threads = [threading.Thread(target=gc), threading.Thread(target=writer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        storage._quarantine = slow  # type: ignore[assignment]
    assert not errors, errors
    with SessionLocal() as db:
        rows = db.query(DatasetVersion).filter(DatasetVersion.dataset_id == b_id).all()
    assert len(rows) == 1
    # B's row exists, so its directory must too, intact.
    assert store.verify(store.open(slug, version)), f"iteration {i}: dangling reference"


@pytest.mark.parametrize("i", range(8))
def test_gc_racing_a_registration_never_leaves_a_row_without_its_directory(client, seeded, i):
    from sqlalchemy import select

    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Job, Workspace

    b_h = register(client, email=f"race{i}@example.com")
    other = uuid.UUID(workspace_id(client, b_h))
    with SessionLocal() as db:
        owner = uuid.UUID(seeded["ws"])
        user = db.scalar(select(Job.created_by).where(Job.workspace_id == owner))
        assert db.get(Workspace, other) is not None
    _race_once(i, owner, other, user)


def test_the_upload_sweep_keeps_a_queued_jobs_files_however_old(client, seeded):
    from sqlalchemy import select

    from quantlab_api.db import SessionLocal
    from quantlab_api.jobs import submit_job, sweep_uploads
    from quantlab_api.models import Dataset, DatasetVersion, Job

    uploads = _settings().data_dir / "uploads"
    queued, abandoned = uploads / "csv-queued-test", uploads / "csv-abandoned-test"
    for d in (queued, abandoned):
        d.mkdir(parents=True, exist_ok=True)
        (d / "bars.csv").write_text("symbol,date\n")
        old = time.time() - 3 * 86400
        os.utime(d, (old, old))
    with SessionLocal() as db:
        ds = db.get(Dataset, db.get(DatasetVersion, uuid.UUID(seeded["dv"])).dataset_id)
        user = db.scalar(select(Job.created_by).where(Job.workspace_id == ds.workspace_id))
        submit_job(
            db,
            workspace_id=ds.workspace_id,
            user_id=user,
            kind="dataset.csv",
            payload={
                "dataset_id": str(ds.id),
                "staged_dir": str(queued),
                "source": "t",
                "license": "t",
                "files": [],
            },
            dispatch=False,
        )
    sweep_uploads()
    assert queued.exists()  # its job is still queued after three days
    assert not abandoned.exists()
