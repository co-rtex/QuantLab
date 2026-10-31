"""Immutable, content-addressed dataset versions stored as Parquet.

Layout::

    <root>/<dataset_slug>/<version_hash>/bars.parquet
                                        /actions.parquet
                                        /instruments.parquet   (optional)
                                        /symbol_history.parquet (optional)
                                        /manifest.json

``version_hash`` is a SHA-256 over a canonical serialization of the data, so the same
logical content always yields the same version and any change yields a new one. A
version directory is never rewritten once written.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quantlab.data.identity import (
    HISTORY_COLUMNS,
    IdentityError,
    is_legacy,
    key_by_instrument,
    legacy_history,
    normalize_history,
)
from quantlab.data.schema import (
    ACTION_COLUMNS,
    ACTION_DATE_COLUMNS,
    normalize_actions,
    normalize_bars,
)

FORMAT_VERSION = 1
ROW_GROUP_ROWS = 50_000


def content_hash(
    bars: pd.DataFrame, actions: pd.DataFrame, symbol_history: pd.DataFrame | None = None
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
    if symbol_history is not None and not is_legacy(symbol_history):
        hist = symbol_history.sort_values(["instrument_id", "valid_from"]).reset_index(drop=True)
        for col in HISTORY_COLUMNS:
            h.update(f"history.{col}".encode())
            h.update(pd.util.hash_pandas_object(hist[col], index=False).to_numpy().tobytes())
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

    def write(
        self,
        slug: str,
        bars: pd.DataFrame,
        actions: pd.DataFrame,
        provenance: dict[str, Any],
        quality: dict[str, Any] | None = None,
        instruments: pd.DataFrame | None = None,
        symbol_history: pd.DataFrame | None = None,
    ) -> DatasetVersion:
        bars = normalize_bars(bars)
        actions = normalize_actions(actions)
        history = None if symbol_history is None else normalize_history(symbol_history)
        if history is not None:
            # Fail before writing anything if the data cannot be keyed by instrument.
            key_by_instrument(bars, actions, history)
        version = content_hash(bars, actions, history)
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
        except IdentityError:  # a history edited into an invalid one was tampered with
            return False
        return content_hash(self.load_bars(dv), self.load_actions(dv), history) == dv.version
