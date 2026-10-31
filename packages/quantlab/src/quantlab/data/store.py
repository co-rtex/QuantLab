"""Immutable, content-addressed dataset versions stored as Parquet.

Layout::

    <root>/<dataset_slug>/<version_hash>/bars.parquet
                                        /actions.parquet
                                        /instruments.parquet   (optional)
                                        /symbol_history.parquet (optional)
                                        /instrument_master.parquet (optional)
                                        /manifest.json

``version_hash`` is a SHA-256 over a canonical serialization of the data, so the same
logical content always yields the same version and any change yields a new one. A
version directory is never rewritten once written.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantlab.data.identity import (
    HISTORY_COLUMNS,
    MASTER_COLUMNS,
    IdentityError,
    is_legacy,
    key_by_instrument,
    legacy_history,
    normalize_history,
    normalize_master,
)
from quantlab.data.schema import (
    ACTION_COLUMNS,
    ACTION_DATE_COLUMNS,
    normalize_actions,
    normalize_bars,
)

FORMAT_VERSION = 1
ROW_GROUP_ROWS = 50_000
# link() errors meaning the filesystem has no hard links (some network and FUSE mounts).
_NO_HARD_LINKS = {errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV, errno.EMLINK}
REPORTS_DIR = "_reports"
_VERSION = re.compile(r"[0-9a-f]{64}")
REPORT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def content_hash(
    bars: pd.DataFrame,
    actions: pd.DataFrame,
    symbol_history: pd.DataFrame | None = None,
    master: pd.DataFrame | None = None,
) -> str:
    h = hashlib.sha256()
    h.update(f"quantlab-dataset-v{FORMAT_VERSION}\n".encode())
    b = bars.sort_values(["symbol", "date"]).reset_index(drop=True)
    for col in ["symbol", "date", "open", "high", "low", "close", "volume", "available_at"]:
        h.update(col.encode())
        h.update(pd.util.hash_pandas_object(b[col], index=False).to_numpy().tobytes())
    a = actions.sort_values(["symbol", "ex_date", "kind"]).reset_index(drop=True)
    # Optional action dates enter the hash only when some are known, so versions written
    # before these columns existed keep their hash.
    dated = [c for c in ACTION_DATE_COLUMNS if c in a.columns and a[c].notna().any()]
    for col in [*ACTION_COLUMNS, *dated]:
        h.update(col.encode())
        h.update(pd.util.hash_pandas_object(a[col], index=False).to_numpy().tobytes())
    # Identity changes what the data means (which series a ticker belongs to), so a real
    # history is part of the version. The legacy identity map is not, which keeps every
    # version written before symbol histories existed at its hash.
    history = None if symbol_history is None else normalize_history(symbol_history)
    if history is not None and not is_legacy(history):
        hist = history.sort_values(["instrument_id", "valid_from"]).reset_index(drop=True)
        for col in HISTORY_COLUMNS:
            h.update(f"history.{col}".encode())
            h.update(pd.util.hash_pandas_object(hist[col], index=False).to_numpy().tobytes())
    # Delisting dates and returns change backtest and ML results, so a stored instrument
    # master is part of the version too (there are no legacy versions with one).
    if master is not None:
        mst = normalize_master(master)
        for col in MASTER_COLUMNS:
            h.update(f"master.{col}".encode())
            h.update(pd.util.hash_pandas_object(mst[col], index=False).to_numpy().tobytes())
    return h.hexdigest()


@dataclass(frozen=True)
class DatasetVersion:
    slug: str
    version: str
    path: Path
    manifest: dict[str, Any]

    @property
    def short(self) -> str:
        return self.version[:12]


class DatasetStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, slug: str, version: str) -> Path:
        if not slug.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"invalid dataset slug {slug!r}")
        if not all(c in "0123456789abcdef" for c in version) or len(version) != 64:
            raise ValueError("invalid version hash")
        return self.root / slug / version

    @staticmethod
    def version_of(
        bars: pd.DataFrame,
        actions: pd.DataFrame,
        symbol_history: pd.DataFrame | None = None,
        master: pd.DataFrame | None = None,
    ) -> str:
        """The version hash :meth:`write` would give these inputs, without writing."""
        history = None if symbol_history is None else normalize_history(symbol_history)
        return content_hash(
            normalize_bars(bars),
            normalize_actions(actions),
            history,
            None if master is None else normalize_master(master),
        )

    def version_paths(self, slug: str, version: str) -> list[tuple[Path, str]]:
        """The version's directory and its reports, with a label for each.

        For a caller that removes them (``quantlab_api.storage`` quarantines them in one
        rename each, under a lock, after deciding nothing references the version). A
        rename never leaves a half-deleted version visible under its name, which ``write``
        would treat as already written.
        """
        return [
            (self._dir(slug, version), f"datasets/{slug}/{version}"),
            (self.root / slug / REPORTS_DIR / version, f"datasets/{slug}/_reports/{version}"),
        ]

    def prune_slug(self, slug: str) -> None:
        """Remove the slug's directories if they are now empty."""
        slug_dir = self.root / slug
        for d in (slug_dir / REPORTS_DIR, slug_dir):
            with contextlib.suppress(OSError):
                d.rmdir()  # only if empty

    def versions_on_disk(self) -> Iterator[tuple[str, str, Path]]:
        """(slug, version, directory) of every published version under the root.

        Directories renamed or removed concurrently are skipped, never raised.
        """
        for slug_dir in _listing(self.root):
            if slug_dir.name.startswith(".") or not slug_dir.is_dir():
                continue
            for d in _listing(slug_dir):
                if _VERSION.fullmatch(d.name) and d.is_dir():
                    yield slug_dir.name, d.name, d

    def stale_temporaries(self, older_than_seconds: float) -> list[Path]:
        """Leftovers of interrupted writes (``.tmp-*``) older than the age.

        Covers version writes (``<slug>/.tmp-*``) and report writes
        (``<slug>/_reports/<version>/.tmp-*``); anything that vanishes meanwhile is skipped.
        """
        cutoff = time.time() - older_than_seconds
        out = []
        for slug_dir in _listing(self.root):
            if slug_dir.name.startswith("."):
                continue
            found = [*_glob(slug_dir, ".tmp-*"), *_glob(slug_dir, f"{REPORTS_DIR}/*/.tmp-*")]
            for d in found:
                try:
                    if d.stat().st_mtime < cutoff:
                        out.append(d)
                except FileNotFoundError:
                    continue
        return sorted(out)

    def write(
        self,
        slug: str,
        bars: pd.DataFrame,
        actions: pd.DataFrame,
        provenance: dict[str, Any],
        quality: dict[str, Any] | None = None,
        instruments: pd.DataFrame | None = None,
        symbol_history: pd.DataFrame | None = None,
        master: pd.DataFrame | None = None,
    ) -> DatasetVersion:
        bars = normalize_bars(bars)
        actions = normalize_actions(actions)
        history = None if symbol_history is None else normalize_history(symbol_history)
        if history is not None:
            # Fail before writing anything if the data cannot be keyed by instrument.
            key_by_instrument(bars, actions, history)
        master = None if master is None else normalize_master(master)
        version = content_hash(bars, actions, history, master)
        target = self._dir(slug, version)
        if target.exists():
            return self.open(slug, version)
        manifest = {
            "format_version": FORMAT_VERSION,
            "slug": slug,
            "version": version,
            "created_at": datetime.now(UTC).isoformat(),
            "rows": len(bars),
            "symbols": sorted(bars["symbol"].unique().tolist()),
            "start": str(bars["date"].min().date()) if len(bars) else None,
            "end": str(bars["date"].max().date()) if len(bars) else None,
            "actions": len(actions),
            "symbol_history": history is not None,
            "instrument_master": master is not None,
            "provenance": provenance,
            "quality": quality,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=target.parent))
        try:
            # Small row groups let DuckDB/PyArrow skip most of the file for single-symbol
            # reads (rows are sorted by symbol): measured 3.4x faster p50 on 2.4M rows,
            # for a ~66% larger file (docs/BENCHMARKS.md).
            bars.to_parquet(
                tmp / "bars.parquet",
                index=False,
                compression="zstd",
                row_group_size=ROW_GROUP_ROWS,
            )
            actions.to_parquet(tmp / "actions.parquet", index=False)
            if instruments is not None:
                instruments.to_parquet(tmp / "instruments.parquet", index=False)
            if history is not None:
                history.to_parquet(tmp / "symbol_history.parquet", index=False)
            if master is not None:
                master.to_parquet(tmp / "instrument_master.parquet", index=False)
            (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
            try:
                os.rename(tmp, target)  # atomic publish of the whole version
            except OSError:
                if not target.exists():
                    raise
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
        return self.open(slug, version)

    def open(self, slug: str, version: str) -> DatasetVersion:
        d = self._dir(slug, version)
        manifest_path = d / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"dataset {slug}@{version[:12]} not found")
        return DatasetVersion(slug, version, d, json.loads(manifest_path.read_text()))

    def versions(self, slug: str) -> list[DatasetVersion]:
        base = self.root / slug
        if not base.exists():
            return []
        out = [self.open(slug, p.name) for p in base.iterdir() if (p / "manifest.json").exists()]
        return sorted(out, key=lambda v: v.manifest["created_at"])

    def load_bars(
        self,
        dv: DatasetVersion,
        symbols: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        filters: list[tuple[str, str, Any]] = []
        if symbols:
            filters.append(("symbol", "in", [s.upper() for s in symbols]))
        if start:
            filters.append(("date", ">=", pd.Timestamp(start)))
        if end:
            filters.append(("date", "<=", pd.Timestamp(end)))
        df = pd.read_parquet(dv.path / "bars.parquet", filters=filters or None)
        df["symbol"] = df["symbol"].astype(str)
        return df.sort_values(["symbol", "date"]).reset_index(drop=True)

    def load_actions(self, dv: DatasetVersion, symbols: list[str] | None = None) -> pd.DataFrame:
        df = pd.read_parquet(dv.path / "actions.parquet")
        if len(df):
            df["symbol"] = df["symbol"].astype(str)
            df["kind"] = df["kind"].astype(str)
        if symbols:
            df = df[df["symbol"].isin([s.upper() for s in symbols])]
        return normalize_actions(df)

    def _reports_dir(self, dv: DatasetVersion) -> Path:
        # Beside the version, never inside it: a version directory is immutable.
        return self._dir(dv.slug, dv.version).parent / REPORTS_DIR / dv.version

    def write_report(
        self, dv: DatasetVersion, name: str, payload: dict[str, Any], *, replace: bool = False
    ) -> Path:
        """Attach a JSON report (e.g. a reconciliation) to a version.

        An existing report of the same name is kept unless ``replace`` is True, so a
        rerun never silently overwrites the evidence of an earlier one. The payload must
        be JSON-native; anything else raises rather than being stringified.
        """
        if not REPORT_NAME.fullmatch(name):
            raise ValueError(f"invalid report name {name!r}")
        target = self._reports_dir(dv)
        path = target / f"{name}.json"
        if path.exists() and not replace:
            raise FileExistsError(f"report {name!r} already exists for {dv.short}")
        text = json.dumps(payload, indent=2, allow_nan=False)
        target.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=target)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            if replace:
                os.replace(tmp, path)  # atomic
            else:
                # link() fails if the name exists, so two concurrent writers cannot both
                # pass the existence check above and overwrite each other.
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    raise
                except OSError as exc:
                    if exc.errno not in _NO_HARD_LINKS:
                        raise
                    # A filesystem without hard links: an exclusive create still refuses
                    # an existing name, though a reader may briefly see a partial file.
                    out = open(path, "x")  # noqa: SIM115 - closed below; raises if taken
                    try:
                        with out:  # a full disk often fails only at the flush on close
                            out.write(text)
                    except BaseException:
                        # A partial report would block the rerun and read as invalid; the
                        # exclusive create above means the file is ours, so remove it.
                        path.unlink(missing_ok=True)
                        raise
                Path(tmp).unlink()
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def load_reports(
        self, dv: DatasetVersion, *, unreadable: list[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """The version's reports by name.

        A file that is not a JSON object raises, unless ``unreadable`` is given: then it is
        skipped and its name appended there, so one corrupt file cannot hide the others.
        """
        d = self._reports_dir(dv)
        if not d.exists():
            return {}
        out: dict[str, dict[str, Any]] = {}
        for p in sorted(d.glob("*.json")):
            try:
                payload = json.loads(p.read_text())
                if not isinstance(payload, dict):
                    raise ValueError(f"report {p.stem!r} is not a JSON object")
            except (ValueError, UnicodeDecodeError):  # JSONDecodeError is a ValueError
                if unreadable is None:
                    raise
                unreadable.append(p.stem)
                continue
            out[p.stem] = payload
        return out

    def load_master(self, dv: DatasetVersion) -> pd.DataFrame | None:
        """The version's instrument master (delisting dates and returns), if it has one."""
        p = dv.path / "instrument_master.parquet"
        return normalize_master(pd.read_parquet(p)) if p.exists() else None

    def load_symbol_history(self, dv: DatasetVersion) -> pd.DataFrame:
        """The version's symbol history, or the legacy identity map when it has none."""
        p = dv.path / "symbol_history.parquet"
        if p.exists():
            return normalize_history(pd.read_parquet(p))
        return legacy_history(dv.manifest["symbols"])

    def load_instruments(self, dv: DatasetVersion) -> pd.DataFrame | None:
        p = dv.path / "instruments.parquet"
        return pd.read_parquet(p) if p.exists() else None

    def verify(self, dv: DatasetVersion) -> bool:
        """Recompute the content hash; False means the stored files were tampered with."""
        try:
            history = self.load_symbol_history(dv)
            master = self.load_master(dv)
        except IdentityError:  # identity data edited into an invalid state was tampered with
            return False
        bars, actions = self.load_bars(dv), self.load_actions(dv)
        return content_hash(bars, actions, history, master) == dv.version


def _listing(root: Path) -> list[Path]:
    """``root``'s entries, or none if it vanished (a concurrent rename or removal)."""
    try:
        return sorted(root.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []


def _glob(root: Path, pattern: str) -> list[Path]:
    try:
        return sorted(root.glob(pattern))
    except FileNotFoundError:
        return []
