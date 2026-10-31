"""Storage lifecycle: files outlive no row and no row outlives its files (#21).

Dataset versions are content-addressed (``datasets/<slug>/<hash>``), so workspaces that
hold the same slug and content share one directory. Artifacts (``experiments/<id>``,
``ml/<id>``, ``staging/<id>-<attempt>``) and staged uploads belong to one row each.

* Deleting a workspace collects what it referenced inside the deleting transaction
  (:func:`workspace_garbage`) and removes the files only after the commit
  (:func:`collect`). A shared version directory is removed only when no
  ``DatasetVersion`` row references it any more, decided under the version's advisory
  lock, which ``register_version`` also holds from before ``store.write`` through its
  commit. So a racing registration either sees the directory gone and writes it again, or
  commits its row first and the collector sees the reference.
* A crash between the commit and the deletion leaves only unreferenced files, never a
  row without its directory. :func:`sweep_orphans`, run by the reaper, removes them once
  they are older than the grace period.

Jobs are still deleted only through the workspace cascade (ADR-0011).
"""

from __future__ import annotations

import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quantlab.data.store import DatasetStore
from quantlab_api.config import get_settings
from quantlab_api.db import SessionLocal
from quantlab_api.models import (
    Dataset,
    DatasetVersion,
    Experiment,
    Job,
    JobStatus,
    MLRun,
)
from quantlab_api.observability import log

# A version is written inside a job, so no write outlives the longest job timeout; the
# sweep's grace period is at least twice that, and never under a day.
MIN_GRACE_SECONDS = 24 * 3600
LIVE = (JobStatus.queued, JobStatus.running)


def version_lock(db: Session, slug: str, version: str) -> None:
    """Hold the version directory's advisory lock until ``db``'s transaction ends."""
    from quantlab_api.jobs import advisory_lock

    advisory_lock(db, f"dataset-dir:{slug}/{version}")


def _references(db: Session, slug: str, version: str) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(DatasetVersion)
            .join(Dataset, Dataset.id == DatasetVersion.dataset_id)
            .where(Dataset.slug == slug, DatasetVersion.version_hash == version)
        )
        or 0
    )


@dataclass
class Garbage:
    """Files a workspace's rows referenced, gathered before its rows are deleted."""

    versions: set[tuple[str, str]] = field(default_factory=set)
    experiments: set[str] = field(default_factory=set)
    ml_runs: set[str] = field(default_factory=set)
    uploads: set[str] = field(default_factory=set)


def workspace_garbage(db: Session, workspace_id: uuid.UUID) -> Garbage:
    """What the workspace's rows point at on disk (call in the deleting transaction)."""
    g = Garbage()
    g.versions = {
        (slug, h)
        for slug, h in db.execute(
            select(Dataset.slug, DatasetVersion.version_hash)
            .join(DatasetVersion, DatasetVersion.dataset_id == Dataset.id)
            .where(Dataset.workspace_id == workspace_id)
        )
    }
    g.experiments = {
        str(i)
        for i in db.scalars(select(Experiment.id).where(Experiment.workspace_id == workspace_id))
    }
    g.ml_runs = {
        str(i) for i in db.scalars(select(MLRun.id).where(MLRun.workspace_id == workspace_id))
    }
    for payload in db.scalars(select(Job.payload).where(Job.workspace_id == workspace_id)):
        staged = (payload or {}).get("staged_dir")
        if staged:
            g.uploads.add(str(staged))
    return g


def _rmtree(path: Path) -> bool:
    if not path.exists():
        return False
    shutil.rmtree(path, ignore_errors=True)
    return True


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _remove_unreferenced(store: DatasetStore, slug: str, version: str) -> bool:
    """Delete one version directory if, under its lock, nothing references it."""
    with SessionLocal() as db:
        version_lock(db, slug, version)
        if _references(db, slug, version):
            return False
        removed = store.remove_version(slug, version)
        db.commit()  # releases the lock
        return removed


def collect(g: Garbage) -> dict[str, int]:
    """Remove what a deleted workspace left behind. Call only after its commit.

    Best effort: a failure is logged and the files are left for :func:`sweep_orphans`.
    """
    settings = get_settings()
    store = DatasetStore(settings.datasets_dir)
    stats = {"versions": 0, "experiments": 0, "ml_runs": 0, "uploads": 0}
    try:
        for slug, version in sorted(g.versions):
            stats["versions"] += _remove_unreferenced(store, slug, version)
        art = settings.artifacts_dir
        for eid in sorted(g.experiments):
            stats["experiments"] += _rmtree(art / "experiments" / eid)
            for staged in (art / "staging").glob(f"{eid}-*"):
                _rmtree(staged)
        for rid in sorted(g.ml_runs):
            stats["ml_runs"] += _rmtree(art / "ml" / rid)
        uploads = settings.data_dir / "uploads"
        for d in sorted(g.uploads):
            if _inside(Path(d), uploads):  # never follow a payload path elsewhere
                stats["uploads"] += _rmtree(Path(d))
    except Exception as exc:  # the sweep collects whatever is left
        log.warning("storage_collect_failed", error=type(exc).__name__)
    log.info("storage_collected", **stats)
    return stats


def grace_seconds(db: Session) -> float:
    """Longer than any write still in progress could take (see ``MIN_GRACE_SECONDS``)."""
    longest = db.scalar(select(func.max(Job.timeout_seconds)).where(Job.status.in_(LIVE)))
    bound = max(int(longest or 0), get_settings().job_default_timeout_seconds)
    return float(max(MIN_GRACE_SECONDS, 2 * bound))


def _old(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime < cutoff
    except FileNotFoundError:
        return False


def sweep_orphans(min_age_seconds: float | None = None) -> dict[str, Any]:
    """Remove files no row references, once older than the grace period.

    A version directory is removed only under its lock and with no reference, so the
    sweep can never leave a row without its directory. ``min_age_seconds`` overrides the
    grace period (tests).
    """
    settings = get_settings()
    stats: dict[str, Any] = {"versions": 0, "temporaries": 0, "experiments": 0, "ml_runs": 0}
    with SessionLocal() as db:
        grace = grace_seconds(db) if min_age_seconds is None else min_age_seconds
    cutoff = time.time() - grace
    if settings.datasets_dir.exists():
        store = DatasetStore(settings.datasets_dir)
        for slug, version, path in list(store.versions_on_disk()):
            if _old(path, cutoff):
                stats["versions"] += _remove_unreferenced(store, slug, version)
        for tmp in store.stale_temporaries(grace):
            stats["temporaries"] += _rmtree(tmp) if tmp.is_dir() else _unlink(tmp)
    art = settings.artifacts_dir
    with SessionLocal() as db:
        for kind, model in (("experiments", Experiment), ("ml_runs", MLRun)):
            root = art / ("experiments" if kind == "experiments" else "ml")
            if not root.exists():
                continue
            for d in root.iterdir():
                if _old(d, cutoff) and not _row_exists(db, model, d.name):
                    stats[kind] += _rmtree(d)
        staging = art / "staging"
        if staging.exists():
            for d in staging.iterdir():
                if _old(d, cutoff) and not _row_exists(db, Experiment, d.name.rsplit("-", 1)[0]):
                    _rmtree(d)
    stats["grace_seconds"] = grace
    return stats


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _row_exists(db: Session, model: Any, name: str) -> bool:
    try:
        key = uuid.UUID(name)
    except ValueError:
        return True  # not ours to judge: leave unknown names alone
    return db.get(model, key) is not None
