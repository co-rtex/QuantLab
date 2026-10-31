"""Read-only access to stored reconciliation reports (#12).

Reports are produced by ``quantlab.data.reconcile`` and stored beside the immutable
dataset version (``DatasetStore.write_report``). These routes only serve what is stored;
nothing is computed on request. Access follows the workspace: viewers and above, and a
dataset version outside the workspace in the path is reported as not found, exactly like
an id that does not exist.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import ValidationError
from sqlalchemy import select

from quantlab.data.store import REPORT_NAME, DatasetStore
from quantlab_api.config import get_settings
from quantlab_api.errors import ApiError
from quantlab_api.models import Dataset, DatasetVersion, User
from quantlab_api.observability import log
from quantlab_api.schemas import ReconciliationOut, ReconciliationSummaryOut
from quantlab_api.security import DB, CurrentUser, require_workspace

router = APIRouter(tags=["reconciliation"])
PREFIX = "reconciliation"  # stored report names start with this


def _reports(
    db: DB, user: User, workspace_id: uuid.UUID, version_id: uuid.UUID, unreadable: list[str]
) -> dict[str, Any]:
    """The version's reconciliation reports; names of unreadable files go to ``unreadable``."""
    require_workspace(db, user, workspace_id)
    row = db.execute(
        select(DatasetVersion, Dataset)
        .join(Dataset, Dataset.id == DatasetVersion.dataset_id)
        .where(DatasetVersion.id == version_id, Dataset.workspace_id == workspace_id)
    ).first()
    if row is None:
        raise ApiError(404, "not_found", "Dataset version not found")
    dv_row, ds = row
    store = DatasetStore(get_settings().datasets_dir)
    try:
        dv = store.open(ds.slug, dv_row.version_hash)
    except FileNotFoundError as exc:
        raise ApiError(404, "not_found", "Dataset version files not found") from exc
    found = store.load_reports(dv, unreadable=unreadable)
    unreadable[:] = [k for k in unreadable if k.startswith(PREFIX)]
    for name in unreadable:
        log.warning("reconciliation_report_unreadable", report=name, version=str(version_id))
    return {k: v for k, v in found.items() if k.startswith(PREFIX)}


@router.get(
    "/workspaces/{workspace_id}/dataset-versions/{version_id}/reconciliations",
    response_model=list[ReconciliationSummaryOut],
)
def list_reconciliations(
    workspace_id: uuid.UUID, version_id: uuid.UUID, user: CurrentUser, db: DB
) -> list[ReconciliationSummaryOut]:
    out = []
    for name, r in sorted(_reports(db, user, workspace_id, version_id, []).items()):
        try:
            out.append(ReconciliationSummaryOut.model_validate({**r, "name": name}))
        except ValidationError:  # one unreadable file must not hide the valid reports
            log.warning("reconciliation_report_invalid", report=name, version=str(version_id))
    return out


@router.get(
    "/workspaces/{workspace_id}/dataset-versions/{version_id}/reconciliations/{name}",
    response_model=ReconciliationOut,
)
def get_reconciliation(
    workspace_id: uuid.UUID, version_id: uuid.UUID, name: str, user: CurrentUser, db: DB
) -> ReconciliationOut:
    if not REPORT_NAME.fullmatch(name):
        raise ApiError(404, "not_found", "Reconciliation report not found")
    unreadable: list[str] = []
    reports = _reports(db, user, workspace_id, version_id, unreadable)
    # A corrupt stored file is the server's fault, not the request's: 500, never 422.
    if name in unreadable:
        raise ApiError(500, "invalid_report", "Stored reconciliation report is unreadable")
    if name not in reports:
        raise ApiError(404, "not_found", "Reconciliation report not found")
    try:
        return ReconciliationOut.model_validate({**reports[name], "name": name})
    except ValidationError as exc:
        raise ApiError(500, "invalid_report", "Stored reconciliation report is malformed") from exc
