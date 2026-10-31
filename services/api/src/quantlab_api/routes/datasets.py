from __future__ import annotations

import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from quantlab.data.synthetic import SyntheticSpec
from quantlab_api.config import get_settings
from quantlab_api.errors import ApiError
from quantlab_api.jobs import redis_conn, submit_job
from quantlab_api.marketdata import bars_payload
from quantlab_api.models import (
    Dataset,
    DatasetVersion,
    Instrument,
    Job,
    Role,
    User,
    WorkspaceMember,
)
from quantlab_api.schemas import (
    MAX_OFFSET,
    BarsOut,
    BoundedDate,
    DatasetOut,
    DatasetVersionDetail,
    DatasetVersionOut,
    InstrumentOut,
    JobOut,
    Page,
    SyntheticDatasetIn,
)
from quantlab_api.security import DB, CurrentUser, audit, require_workspace
from quantlab_api.uploads import read_multipart

router = APIRouter(tags=["datasets"])


def load_version(db: DB, user: User, version_id: uuid.UUID) -> tuple[DatasetVersion, Dataset]:
    """Authorize access to a dataset version through workspace membership."""
    row = db.execute(
        select(DatasetVersion, Dataset)
        .join(Dataset, Dataset.id == DatasetVersion.dataset_id)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Dataset.workspace_id)
        .where(DatasetVersion.id == version_id, WorkspaceMember.user_id == user.id)
    ).first()
    if row is None:
        raise ApiError(404, "not_found", "Dataset version not found")
    return row[0], row[1]


@router.get("/workspaces/{workspace_id}/datasets", response_model=list[DatasetOut])
def list_datasets(workspace_id: uuid.UUID, user: CurrentUser, db: DB) -> list[Dataset]:
    require_workspace(db, user, workspace_id)
    return list(
        db.scalars(
            select(Dataset).where(Dataset.workspace_id == workspace_id).order_by(Dataset.created_at)
        )
    )


@router.post(
    "/workspaces/{workspace_id}/datasets/synthetic", response_model=JobOut, status_code=202
)
def create_synthetic(
    workspace_id: uuid.UUID,
    body: SyntheticDatasetIn,
    user: CurrentUser,
    db: DB,
    request: Request,
    idempotency_key: str | None = Header(None, min_length=1, max_length=128),
) -> JobOut:
    require_workspace(db, user, workspace_id, Role.editor)
    if body.end <= body.start:
        raise ApiError(422, "validation_error", "end must be after start")
    if (body.end - body.start).days > 366 * 30:
        raise ApiError(422, "validation_error", "date range is limited to 30 years")
    est_rows = (body.n_assets + 1) * (body.end - body.start).days * 252 // 365
    if est_rows > get_settings().max_synthetic_rows:
        raise ApiError(
            422,
            "dataset_too_large",
            f"about {est_rows:,} rows requested; the limit is "
            f"{get_settings().max_synthetic_rows:,}",
        )
    ds = db.scalar(
        select(Dataset).where(Dataset.workspace_id == workspace_id, Dataset.slug == body.slug)
    )
    if ds is None:
        ds = Dataset(
            workspace_id=workspace_id,
            slug=body.slug,
            name=body.name,
            description=body.description
            or "Deterministic synthetic market data (not real observations).",
            provider="synthetic",
            created_by=user.id,
        )
        db.add(ds)
        db.flush()
    elif ds.provider != "synthetic":
        raise ApiError(409, "slug_taken", "A non-synthetic dataset already uses this slug")
    spec = SyntheticSpec(
        n_assets=body.n_assets, start=str(body.start), end=str(body.end), seed=body.seed
    )
    payload = {
        "dataset_id": str(ds.id),
        "spec": {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(spec).items()},
    }
    audit(
        db,
        request,
        "dataset.ingest_requested",
        user=user,
        workspace_id=workspace_id,
        target_type="dataset",
        target_id=ds.id,
        provider="synthetic",
    )
    job, _ = submit_job(
        db,
        workspace_id=workspace_id,
        user_id=user.id,
        kind="dataset.synthetic",
        payload=payload,
        idempotency_key=idempotency_key,
        request_id=getattr(request.state, "request_id", None),
    )
    return JobOut.model_validate(job)


@router.get("/datasets/{dataset_id}/versions", response_model=list[DatasetVersionOut])
def list_versions(dataset_id: uuid.UUID, user: CurrentUser, db: DB) -> list[DatasetVersion]:
    ds = db.get(Dataset, dataset_id)
    if ds is None:
        raise ApiError(404, "not_found", "Dataset not found")
    require_workspace(db, user, ds.workspace_id)
    return list(
        db.scalars(
            select(DatasetVersion)
            .where(DatasetVersion.dataset_id == ds.id)
            .order_by(DatasetVersion.created_at)
        )
    )


@router.get("/workspaces/{workspace_id}/dataset-versions", response_model=list[DatasetVersionOut])
def workspace_versions(workspace_id: uuid.UUID, user: CurrentUser, db: DB) -> list[DatasetVersion]:
    require_workspace(db, user, workspace_id)
    return list(
        db.scalars(
            select(DatasetVersion)
            .join(Dataset, Dataset.id == DatasetVersion.dataset_id)
            .where(Dataset.workspace_id == workspace_id)
            .order_by(DatasetVersion.created_at.desc())
        )
    )


@router.get("/dataset-versions/{version_id}", response_model=DatasetVersionDetail)
def get_version(version_id: uuid.UUID, user: CurrentUser, db: DB) -> DatasetVersion:
    return load_version(db, user, version_id)[0]


@router.get("/dataset-versions/{version_id}/instruments", response_model=Page[InstrumentOut])
def instruments(
    version_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    q: str | None = Query(None, max_length=32),
    sector: str | None = Query(None, max_length=80),
    limit: int = Query(100, ge=1, le=2000),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
) -> Page[InstrumentOut]:
    dv, _ = load_version(db, user, version_id)
    stmt = select(Instrument).where(Instrument.dataset_version_id == dv.id)
    if q:
        stmt = stmt.where(Instrument.symbol.ilike(f"{q.upper()}%"))
    if sector:
        stmt = stmt.where(Instrument.sector == sector)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Instrument.symbol).limit(limit).offset(offset)).all()
    return Page(
        items=[InstrumentOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/dataset-versions/{version_id}/bars/{symbol}", response_model=BarsOut)
def bars(
    version_id: uuid.UUID,
    symbol: str,
    user: CurrentUser,
    db: DB,
    start: BoundedDate | None = None,
    end: BoundedDate | None = None,
    adjusted: bool = False,
) -> BarsOut:
    dv, _ = load_version(db, user, version_id)
    if not symbol.replace(".", "").replace("-", "").isalnum() or len(symbol) > 32:
        raise ApiError(422, "validation_error", "invalid symbol")
    exists = db.scalar(
        select(Instrument.id).where(
            Instrument.dataset_version_id == dv.id, Instrument.symbol == symbol.upper()
        )
    )
    if exists is None:
        raise ApiError(404, "not_found", f"{symbol.upper()} is not in this dataset version")
    try:
        redis = redis_conn()
    except Exception:
        redis = None
    from pathlib import Path

    payload = bars_payload(
        Path(dv.storage_path), dv.version_hash, symbol, start, end, adjusted, redis
    )
    return BarsOut(is_synthetic=dv.is_synthetic, **payload)


MAX_UPLOAD_BYTES = 50 * 2**20
# Generous byte caps for the text fields; CsvUploadForm enforces the character limits.
MAX_FIELD_BYTES = 2048


class CsvUploadForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")
    name: str = Field(min_length=1, max_length=120)
    source: str = Field(min_length=3, max_length=300)
    license: str = Field(min_length=3, max_length=300)


def _csv_upload_openapi() -> dict[str, Any]:
    schema = CsvUploadForm.model_json_schema()
    schema.pop("additionalProperties", None)
    schema["properties"]["bars"] = {"type": "string", "format": "binary"}
    schema["properties"]["actions"] = {"type": "string", "format": "binary"}
    schema["required"].append("bars")
    content = {"multipart/form-data": {"schema": schema}}
    return {"requestBody": {"required": True, "content": content}}


# The body is read by hand, after authorization: FastAPI parses declared Form/File
# parameters before it runs any dependency, so declaring them let anonymous clients make
# the API spool up to 110 MB per request (#4). See quantlab_api/uploads.py.
@router.post(
    "/workspaces/{workspace_id}/datasets/csv",
    response_model=JobOut,
    status_code=202,
    openapi_extra=_csv_upload_openapi(),
)
async def upload_csv(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    request: Request,
) -> JobOut:
    """Import user-supplied daily bars (columns: symbol,date,open,high,low,close,volume)
    and optional corporate actions (symbol,ex_date,kind,value). The uploader states the
    data source and licence, which are recorded in the dataset provenance."""
    import shutil
    import tempfile

    require_workspace(db, user, workspace_id, Role.editor)
    # Return the connection to the pool while the body streams: a slow upload must not
    # hold one, or a few dozen trickling uploads would exhaust the pool for everyone.
    db.commit()
    staging_root = get_settings().data_dir / "uploads"
    staging_root.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix="csv-", dir=staging_root))
    # Every rejection after staging (job cap 429, slug 409, errors) must delete the files;
    # only an accepted job owns them. Otherwise each rejected retry left up to 100 MB on
    # disk until the reaper's 24-hour sweep.
    commit_attempted = False
    try:
        upload = await read_multipart(
            request,
            files={
                "bars": (staged / "bars.csv", MAX_UPLOAD_BYTES),
                "actions": (staged / "actions.csv", MAX_UPLOAD_BYTES),
            },
            fields=dict.fromkeys(CsvUploadForm.model_fields, MAX_FIELD_BYTES),
        )
        errors: list[Any] = []
        try:
            form = CsvUploadForm.model_validate(upload.fields)
        except ValidationError as exc:
            errors = [{**e, "loc": ("body", *e["loc"])} for e in exc.errors()]
        if "bars" not in upload.files:
            errors.append({"loc": ("body", "bars"), "msg": "Field required", "type": "missing"})
        if errors:
            raise RequestValidationError(errors)
        # Authorized again: membership may have been revoked while the body streamed.
        require_workspace(db, user, workspace_id, Role.editor)
        slug, name, source, license = form.slug, form.name, form.source, form.license
        files = upload.files
        ds = db.scalar(
            select(Dataset).where(Dataset.workspace_id == workspace_id, Dataset.slug == slug)
        )
        if ds is None:
            ds = Dataset(
                workspace_id=workspace_id,
                slug=slug,
                name=name,
                description=f"Imported from CSV. Source: {source}. Licence: {license}.",
                provider="csv",
                created_by=user.id,
            )
            db.add(ds)
            db.flush()
        elif ds.provider != "csv":
            raise ApiError(409, "slug_taken", "A dataset from another provider uses this slug")
        audit(
            db,
            request,
            "dataset.ingest_requested",
            user=user,
            workspace_id=workspace_id,
            target_type="dataset",
            target_id=ds.id,
            provider="csv",
            bytes=sum(files.values()),
        )
        commit_attempted = True
        job, _ = submit_job(
            db,
            workspace_id=workspace_id,
            user_id=user.id,
            kind="dataset.csv",
            payload={
                "dataset_id": str(ds.id),
                "staged_dir": str(staged),
                "source": source,
                "license": license,
                "files": files,
            },
            request_id=getattr(request.state, "request_id", None),
        )
    except BaseException:
        # A commit can fail on the client after the server applied it (the connection
        # dropped while it answered). Then a committed job still needs these files, so
        # they are kept unless it is certain that none does (#7).
        if not commit_attempted or _staging_unclaimed(staged):
            shutil.rmtree(staged, ignore_errors=True)
        raise
    return JobOut.model_validate(job)


def _staging_unclaimed(staged: Path) -> bool:
    """True only if a fresh look at the database shows no job using ``staged``. On any
    doubt (the database is unreachable) the files stay, for the reaper's sweep."""
    from quantlab_api.db import SessionLocal

    try:
        with SessionLocal() as fresh:
            claimed = fresh.scalar(
                select(Job.id).where(
                    Job.kind == "dataset.csv",
                    Job.payload["staged_dir"].as_string() == str(staged),
                )
            )
    except Exception:
        return False
    return claimed is None
