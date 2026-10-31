"""Synthetic labels for rows that reference a dataset version (#102).

Experiments, ML runs and portfolios each name the dataset version they use. The version
is the authority on whether its data is synthetic, so responses carry its flag with every
row. A version that cannot be found gives ``None`` ("unknown"), never ``False``: the web
then labels the row conservatively instead of presenting it as real data.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from quantlab_api.models import DatasetVersion


def synthetic_flags(db: Session, version_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, bool | None]:
    """``is_synthetic`` of each version id, ``None`` where the version is missing.

    One query for the whole page, however many rows reference it.
    """
    ids = set(version_ids)
    if not ids:
        return {}
    found = {
        vid: flag
        for vid, flag in db.execute(
            select(DatasetVersion.id, DatasetVersion.is_synthetic).where(DatasetVersion.id.in_(ids))
        )
    }
    return {i: (bool(found[i]) if i in found else None) for i in ids}


def is_synthetic(db: Session, version_id: uuid.UUID) -> bool | None:
    return synthetic_flags(db, [version_id])[version_id]
