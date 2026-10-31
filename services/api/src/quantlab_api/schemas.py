"""Request/response models for the v1 API."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from quantlab_api.jobs import SAFE_ERROR_TYPES

MIN_DATE, MAX_DATE = date(1900, 1, 1), date(2200, 12, 31)
BoundedDate = Annotated[date, Field(ge=MIN_DATE, le=MAX_DATE)]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# Deepest page a list endpoint serves; larger offsets overflowed bigint and returned 500.
MAX_OFFSET = 1_000_000


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------- auth
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    display_name: str = Field(min_length=1, max_length=120)

    @field_validator("password")
    @classmethod
    def _strength(cls, v: str) -> str:
        if v.lower() == v or v.upper() == v or not any(c.isdigit() for c in v):
            raise ValueError("password needs upper and lower case letters and a digit")
        return v


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserOut(ORM):
    id: uuid.UUID
    email: str
    display_name: str
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_at: datetime
    user: UserOut


# ---------------------------------------------------------------- workspaces
class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field("", max_length=2000)


class WorkspaceOut(ORM):
    id: uuid.UUID
    name: str
    description: str
    created_at: datetime
    role: str | None = None


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    role: str


# ---------------------------------------------------------------- datasets
class SyntheticDatasetIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field("", max_length=2000)
    n_assets: int = Field(50, ge=2, le=2000)
    start: BoundedDate = date(2018, 1, 2)
    end: BoundedDate = date(2023, 12, 29)
    seed: int = Field(7, ge=0, le=2**31 - 1)


class DatasetOut(ORM):
    id: uuid.UUID
    workspace_id: uuid.UUID
    slug: str
    name: str
    description: str
    provider: str
    created_at: datetime


class DatasetVersionOut(ORM):
    id: uuid.UUID
    dataset_id: uuid.UUID
    version_hash: str
    rows: int
    symbol_count: int
    start_date: date | None
    end_date: date | None
    is_synthetic: bool
    quality_status: str
    created_at: datetime


class DatasetVersionDetail(DatasetVersionOut):
    provenance: dict[str, Any]
    quality: dict[str, Any]


class InstrumentOut(ORM):
    symbol: str
    name: str
    sector: str | None
    asset_class: str
    first_date: date | None
    last_date: date | None
    delisted: bool
    rows: int
    # Additive (#9): permanent identity and delisting facts; null without identity data.
    instrument_id: str | None = None
    delisting_date: date | None = None
    delisting_reason: str | None = None
    delisting_return: float | None = None


class BarsOut(BaseModel):
    symbol: str
    adjusted: bool
    is_synthetic: bool
    dates: list[str]
    open: list[float | None]
    high: list[float | None]
    low: list[float | None]
    close: list[float | None]
    volume: list[float | None]
    cache: str


# ---------------------------------------------------------------- strategies
class StrategyIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    strategy_key: str
    params: dict[str, Any] = Field(default_factory=dict)
    description: str = Field("", max_length=2000)


class StrategyOut(ORM):
    id: uuid.UUID
    name: str
    strategy_key: str
    params: dict[str, Any]
    description: str
    created_at: datetime


# ---------------------------------------------------------------- experiments / jobs
class ExperimentIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    dataset_version_id: uuid.UUID
    strategy_key: str | None = None
    strategy_params: dict[str, Any] = Field(default_factory=dict)
    strategy_id: uuid.UUID | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    notes: str = Field("", max_length=5000)


INTERNAL_PAYLOAD_KEYS = {"staged_dir", "files"}


class JobOut(ORM):
    id: uuid.UUID
    kind: str
    status: str
    progress: float
    message: str
    attempts: int
    max_attempts: int
    cancel_requested: bool
    error: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    payload: dict[str, Any]

    @field_validator("error")
    @classmethod
    def _public_error(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        """Tracebacks and raw exception detail stay in server logs."""
        if v is None:
            return None
        kind = v.get("type")
        message = (
            str(v.get("message", ""))[:300]
            if kind in SAFE_ERROR_TYPES
            else (
                "Internal error while running the job; operators can find details in the "
                "server logs by job id."
            )
        )
        return {"type": kind, "message": message, "retryable": v.get("retryable")}

    @field_validator("payload")
    @classmethod
    def _public_payload(cls, v: dict[str, Any]) -> dict[str, Any]:
        return {k: val for k, val in v.items() if k not in INTERNAL_PAYLOAD_KEYS}


class ExperimentOut(ORM):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    kind: str
    dataset_version_id: uuid.UUID
    strategy_key: str
    strategy_params: dict[str, Any]
    config: dict[str, Any]
    input_hash: str
    code_version: str
    engine_version: str
    parent_id: uuid.UUID | None
    status: str
    notes: str
    created_at: datetime
    summary: dict[str, Any] | None = None
    fingerprint: str | None = None
    # Additive (#102): from the referenced dataset version; None when it is missing.
    is_synthetic: bool | None = None


class ExperimentSubmitted(BaseModel):
    experiment: ExperimentOut
    job: JobOut
    created: bool


class CompareIn(BaseModel):
    experiment_ids: list[uuid.UUID] = Field(min_length=1, max_length=10)


class AuditOut(ORM):
    id: uuid.UUID
    user_id: uuid.UUID | None
    action: str
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]
    request_id: str | None
    created_at: datetime


class ReconciliationSummaryOut(BaseModel):
    """A stored reconciliation report, without its samples (#12)."""

    name: str
    status: Literal["pass", "warn", "fail"]
    created_at: str
    primary: str
    secondary: str
    synthetic_inputs: bool
    counts: dict[str, int]


class ReconciliationOut(ReconciliationSummaryOut):
    inputs: dict[str, Any]
    tolerances: dict[str, Any]
    samples: dict[str, list[dict[str, Any]]]
    instruments: dict[str, int]
