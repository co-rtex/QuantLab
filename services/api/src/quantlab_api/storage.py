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
* Nothing is deleted outright: removed files move to ``<data_dir>/.trash/<timestamp>/``
  and are purged only after ``storage_trash_retention_seconds``. The sweep also refuses
  to act when the database looks wrong for the disk (no version rows but versions on
  disk, as during a restore or against the wrong database) or when one cycle would
  remove more than ``storage_sweep_max_fraction`` of what is on disk. Advisory locks are
  per database, so two stacks sharing a data directory are not protected by them; this
  refusal and the quarantine are what keep such mistakes recoverable.

Jobs are still deleted only through the workspace cascade (ADR-0011).
"""

from __future__ import annotations

import errno
import os
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quantlab.data.store import DatasetStore
from quantlab_api.config import get_settings
from quantlab_api.db import SessionLocal
from quantlab_api.models import Dataset, DatasetVersion, Experiment, Job, JobStatus, MLRun
from quantlab_api.observability import log

# A version is written inside a job, so no write outlives the longest job timeout; the
# sweep's grace period is at least twice that, and never under a day.
MIN_GRACE_SECONDS = 24 * 3600
LIVE = (JobStatus.queued, JobStatus.running)
TRASH = ".trash"
LAST_SWEEP = ".last-storage-sweep"
LAST_PURGE = ".last-trash-purge"


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


def _warn(event: str, item: str, exc: BaseException) -> None:
    """Log a failure with enough context to act on, but no absolute paths."""
    code = errno.errorcode.get(getattr(exc, "errno", None) or -1)
    log.warning(event, item=item, error=type(exc).__name__, errno=code)


# ------------------------------------------------------------------- quarantine
def _trash_dir() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    d = get_settings().data_dir / TRASH / stamp
    d.mkdir(parents=True, exist_ok=True)
    return d


def _quarantine(path: Path, trash: Path, label: str) -> bool:
    """Move ``path`` into ``trash`` in one rename. False if it is gone or cannot move.

    Never raises: one failure (``EXDEV`` when ``.trash`` is on another mount, say) is
    logged and leaves the file in place, without aborting the rest of the cycle.
    """
    try:
        # URL-encoded, so the original path is recoverable even when names contain "_".
        os.rename(path, trash / f"{quote(label, safe='')}-{uuid.uuid4().hex[:8]}")
    except FileNotFoundError:
        return False
    except OSError as exc:
        _warn("storage_quarantine_failed", label, exc)
        return False
    return True


def purge_trash(retention_seconds: float | None = None) -> int:
    """Delete quarantined files older than the retention period."""
    root = get_settings().data_dir / TRASH
    keep = retention_seconds
    if keep is None:
        keep = get_settings().storage_trash_retention_seconds
    cutoff = time.time() - keep
    purged = 0
    try:
        batches = list(root.iterdir())
    except FileNotFoundError:
        return 0
    for d in batches:
        if _old(d, cutoff):
            shutil.rmtree(d, ignore_errors=True)
            purged += 1
    return purged


# ------------------------------------------------------------- workspace delete
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


def _inside(path: Path, root: Path) -> bool:
    """Strictly below ``root`` (never ``root`` itself or anything outside it)."""
    try:
        rel = path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return rel != Path(".")


def _remove_unreferenced(store: DatasetStore, slug: str, version: str, trash: Path) -> bool:
    """Quarantine one version directory if, under its lock, nothing references it."""
    with SessionLocal() as db:
        version_lock(db, slug, version)
        if _references(db, slug, version):
            return False
        moved = False
        for path, label in store.version_paths(slug, version):
            moved |= _quarantine(path, trash, label)
        store.prune_slug(slug)
        db.commit()  # releases the lock
        return moved


def collect(g: Garbage) -> dict[str, int]:
    """Remove what a deleted workspace left behind. Call only after its commit.

    Never raises: each item is attempted on its own, failures are logged and left for
    :func:`sweep_orphans`, so a disk problem cannot turn a committed delete into a 500.
    """
    stats = {"versions": 0, "experiments": 0, "ml_runs": 0, "uploads": 0, "failed": 0}

    def attempt(kind: str, item: str, fn: Callable[[], bool]) -> None:
        try:
            stats[kind] += fn()
        except Exception as exc:
            stats["failed"] += 1
            _warn("storage_collect_failed", f"{kind}:{item}", exc)

    try:
        settings = get_settings()
        store = DatasetStore(settings.datasets_dir)
        trash = _trash_dir()
    except Exception as exc:
        _warn("storage_collect_failed", "setup", exc)
        return stats
    art = settings.artifacts_dir

    def move(path: Path, label: str) -> Callable[[], bool]:
        return lambda: _quarantine(path, trash, label)

    def unreferenced(slug: str, version: str) -> Callable[[], bool]:
        return lambda: _remove_unreferenced(store, slug, version, trash)

    for slug, version in sorted(g.versions):
        attempt("versions", f"{slug}/{version[:12]}", unreferenced(slug, version))
    for eid in sorted(g.experiments):
        attempt("experiments", eid, move(art / "experiments" / eid, f"experiments/{eid}"))
        for staged in _glob(art / "staging", f"{eid}-*"):
            attempt("experiments", staged.name, move(staged, f"staging/{staged.name}"))
    for rid in sorted(g.ml_runs):
        attempt("ml_runs", rid, move(art / "ml" / rid, f"ml/{rid}"))
    uploads = settings.data_dir / "uploads"
    for d in sorted(g.uploads):
        if _inside(Path(d), uploads):  # never follow a payload path elsewhere
            attempt("uploads", Path(d).name, move(Path(d), f"uploads/{Path(d).name}"))
    log.info("storage_collected", **stats)
    return stats


# ------------------------------------------------------------------------ sweep
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


def _glob(root: Path, pattern: str) -> list[Path]:
    try:
        return sorted(root.glob(pattern))
    except FileNotFoundError:
        return []


def _children(root: Path) -> list[Path]:
    try:
        return sorted(root.iterdir())
    except FileNotFoundError:
        return []


def _due(interval: float, name: str = LAST_SWEEP) -> bool:
    """At most one run per ``interval`` across processes (a marker file's mtime).

    A marker dated in the future (the clock stepped back) counts as due, so the run is
    never frozen until the clock catches up.
    """
    marker = get_settings().data_dir / name
    now = time.time()
    try:
        last = marker.stat().st_mtime
    except FileNotFoundError:
        last = None
    if last is None or last < now - interval or last > now + 60:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return True
    return False


def purge_trash_if_due() -> int:
    """The reaper's hourly purge, independent of ``storage_sweep``.

    ``collect`` quarantines on every workspace delete, so the trash must be emptied even
    while the sweep is switched off (as during maintenance), or it would fill the disk.
    """
    if not _due(get_settings().storage_sweep_interval_seconds, LAST_PURGE):
        return 0
    return purge_trash()


def _refuse(kind: str, on_disk: int, rows: int, candidates: int, max_fraction: float) -> bool:
    """Whether a cycle that would remove ``candidates`` of ``on_disk`` looks unsafe."""
    reason = None
    if on_disk and rows == 0:
        reason = "the database has no rows but the disk has files (restore or wrong database?)"
    elif candidates > max(1, max_fraction * on_disk):
        reason = f"more than {max_fraction:.0%} of what is on disk would go in one cycle"
    if reason:
        log.error(
            "storage_sweep_refused",
            kind=kind,
            on_disk=on_disk,
            rows=rows,
            candidates=candidates,
            reason=reason,
            action="nothing removed; check the database, or run sweep_orphans(force=True)",
        )
        return True
    return False


def sweep_orphans(
    min_age_seconds: float | None = None, *, force: bool = False, throttle: bool = True
) -> dict[str, Any]:
    """Quarantine files no row references, once older than the grace period.

    Runs at most once per ``storage_sweep_interval_seconds`` and not at all when
    ``storage_sweep`` is off. A version directory is moved only under its lock and with no
    reference, so the sweep never leaves a row without its directory. It refuses (and
    logs an error) when the database has no rows for files on disk, or when it would
    remove more than ``storage_sweep_max_fraction`` of them at once; ``force`` skips that
    check, the interval and the setting; ``throttle=False`` skips only the interval.
    ``min_age_seconds`` overrides the grace period.
    """
    settings = get_settings()
    stats: dict[str, Any] = {"versions": 0, "temporaries": 0, "experiments": 0, "ml_runs": 0}
    enabled = settings.storage_sweep and (
        not throttle or _due(settings.storage_sweep_interval_seconds)
    )
    if not force and not enabled:
        stats["skipped"] = True
        return stats
    frac = settings.storage_sweep_max_fraction
    with SessionLocal() as db:
        grace = grace_seconds(db) if min_age_seconds is None else min_age_seconds
        # One query per kind, not one per directory (the reaper runs every few seconds).
        referenced = {
            (str(slug), str(h))
            for slug, h in db.execute(
                select(Dataset.slug, DatasetVersion.version_hash)
                .join(DatasetVersion, DatasetVersion.dataset_id == Dataset.id)
                .distinct()
            )
        }
        experiments = {str(i) for i in db.scalars(select(Experiment.id))}
        ml_runs = {str(i) for i in db.scalars(select(MLRun.id))}
    cutoff = time.time() - grace
    trash: Path | None = None

    def bin_() -> Path:
        nonlocal trash
        trash = trash or _trash_dir()
        return trash

    if settings.datasets_dir.exists():
        store = DatasetStore(settings.datasets_dir)
        on_disk = list(store.versions_on_disk())
        candidates = [
            (slug, v)
            for slug, v, path in on_disk
            if (slug, v) not in referenced and _old(path, cutoff)
        ]
        if force or not _refuse("versions", len(on_disk), len(referenced), len(candidates), frac):
            for slug, version in candidates:
                try:
                    stats["versions"] += _remove_unreferenced(store, slug, version, bin_())
                except Exception as exc:
                    _warn("storage_sweep_failed", f"versions:{slug}/{version[:12]}", exc)
        for tmp in store.stale_temporaries(grace):
            stats["temporaries"] += _quarantine(tmp, bin_(), f"datasets-tmp/{tmp.name}")
    art = settings.artifacts_dir
    kinds = (("experiments", "experiments", experiments), ("ml_runs", "ml", ml_runs))
    for kind, sub, rows in kinds:
        dirs = _children(art / sub)
        orphans = [d for d in dirs if _orphan(d.name, rows) and _old(d, cutoff)]
        leftovers = [d for d in dirs if ".old-" in d.name and _old(d, cutoff)]  # promotions
        if force or not _refuse(kind, len(dirs), len(rows), len(orphans), frac):
            for d in orphans:
                stats[kind] += _quarantine(d, bin_(), f"{sub}/{d.name}")
        for d in leftovers:
            stats[kind] += _quarantine(d, bin_(), f"{sub}/{d.name}")
    for d in _children(art / "staging"):
        if _old(d, cutoff) and _orphan(d.name.rsplit("-", 1)[0], experiments):
            _quarantine(d, bin_(), f"staging/{d.name}")
    stats["grace_seconds"] = grace
    return stats


def _orphan(name: str, rows: set[str]) -> bool:
    """A directory named by a row id that no longer exists (other names are left alone)."""
    try:
        key = str(uuid.UUID(name))
    except ValueError:
        return False
    return key not in rows
