"""Runtime configuration from environment variables (prefix ``QUANTLAB_``)."""

from __future__ import annotations

import fcntl
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET_FILE = ".jwt-dev-secret"  # noqa: S105 - a file name, not a secret


def _shared_dev_secret(data_dir: Path) -> SecretStr:
    """A development signing key shared by every process using ``data_dir``.

    A random per-process key breaks as soon as the API runs more than one worker
    (``uvicorn --workers 4``): a token signed by one worker is rejected by the others, so
    roughly three requests in four fail. Processes take an exclusive lock on a separate
    lock file, so exactly one of them creates (or repairs) the key while the others wait
    and then read it. The key is written to a private temporary file and renamed into
    place, so even a reader that ignores the lock never sees half a key.

    The lock needs a filesystem with working ``flock``: some FUSE and SMB mounts lack it
    (this then fails loudly), and NFS without coherent locking could still let processes
    write different keys. Keep ``data_dir`` on a local disk or volume in development.

    The key lives in ``data_dir`` and so travels with its backups. Delete the file to rotate
    it (everyone is signed out). A file readable by others is refused, not repaired: the key
    may already have been read.
    """
    path = data_dir / DEV_SECRET_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            return SecretStr(_create_dev_secret(path))
        if mode & 0o077:
            raise RuntimeError(
                f"{path} is readable by other users (mode {mode & 0o777:o}), so the signing "
                "key may be known. Delete it to rotate the key (signs everyone out)."
            )
        value = path.read_text().strip()
        # Only a crash in the old, non-atomic writer could leave an empty file. Under the
        # lock, replacing it cannot race another process doing the same (#64 review).
        return SecretStr(value or _create_dev_secret(path))
    finally:
        os.close(lock_fd)  # releases the lock


def _create_dev_secret(path: Path) -> str:
    """Write a new key atomically (temporary file, fsync, rename). Call under the lock.

    A rename, unlike a hard link, works on every filesystem (#64 review: os.link fails
    with EPERM or ENOTSUP on some mounts).
    """
    value = secrets.token_urlsafe(48)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return value


# The published development default in docker-compose.yml; refused in production.
DEV_REDIS_PASSWORD = "quantlab-dev"  # noqa: S105


class Settings(BaseSettings):
    # hide_input_in_errors: a failed guard must not print the raw settings, secrets
    # included, into a startup log (#95 review: the key sat in the visible tail).
    model_config = SettingsConfigDict(
        env_prefix="QUANTLAB_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://quantlab:quantlab@localhost:5433/quantlab"
    redis_url: str = "redis://localhost:6380/0"
    data_dir: Path = Path("var/data")
    # Production must set one. Elsewhere an unset secret is generated once and shared by all
    # processes through ``data_dir`` (see _shared_dev_secret).
    jwt_secret: SecretStr | None = None
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = Field(60, ge=1, le=24 * 60)
    cookie_secure: bool = False
    cors_origins: list[str] = ["http://localhost:3000"]
    queue_name: str = "quantlab"
    job_lease_seconds: int = Field(60, ge=5)
    job_default_timeout_seconds: int = Field(900, ge=10)
    job_max_attempts: int = Field(3, ge=1, le=10)
    max_backtest_symbols: int = Field(1500, ge=1)
    # Estimated peak memory one backtest may need (#163, limits.backtest_cost): 80% of the
    # compose worker's 1536 MB memory limit, i.e. 20% headroom over the measured envelope.
    # Raise it together with the worker's limit, never alone.
    max_backtest_memory_mb: int = Field(1228, ge=64)
    max_dataset_assets: int = Field(2000, ge=1)
    rate_limit_login_per_minute: int = Field(10, ge=1)
    # Peers allowed to state the client address in X-Forwarded-For: IPs, CIDRs or hostnames
    # (e.g. the compose service "web"). Requests from anyone else use the socket address.
    trusted_proxies: list[str] = []
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-opus-5-5"
    git_sha: str = "unknown"
    # When set, /metrics requires "Authorization: Bearer <token>" (scrapers only).
    metrics_token: SecretStr | None = None
    max_synthetic_rows: int = Field(5_000_000, ge=1000)
    max_active_jobs_per_workspace: int = Field(50, ge=1)
    # Account-wide limits prevent multiplying the workspace job cap by making new
    # workspaces. Both caps are enforced transactionally under row locks.
    max_active_jobs_per_user: int = Field(50, ge=1)
    max_workspaces_per_user: int = Field(5, ge=1)
    # Licensed market data (#11). Inert unless a registered provider is named AND its key,
    # licence and plan are set: the vendor and licence are the user's choice. The key is
    # read only from the environment or secrets, never stored in a job payload or logged.
    market_data_provider: str | None = None
    market_data_api_key: SecretStr | None = None
    market_data_license: str | None = None  # recorded in every version's provenance
    market_data_plan: str | None = None
    market_data_requests_per_minute: float | None = Field(None, gt=0)
    # Orphan sweep (#21). It quarantines rather than deletes, refuses a cycle that would
    # remove more than this share of what is on disk (or anything when the database has no
    # rows: a restore in progress, the wrong database), and can be switched off.
    storage_sweep: bool = True
    storage_sweep_interval_seconds: int = Field(3600, ge=60)
    storage_sweep_max_fraction: float = Field(0.25, gt=0, le=1)
    storage_trash_retention_seconds: int = Field(7 * 24 * 3600, ge=3600)

    @field_validator(
        "metrics_token", "anthropic_api_key", "jwt_secret", "market_data_api_key", mode="before"
    )
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        """Compose passes unset variables as empty strings; treat those as not configured."""
        if v is None:
            return None
        raw = v.get_secret_value() if isinstance(v, SecretStr) else str(v)
        return None if not raw.strip() else v

    @field_validator(
        "market_data_provider", "market_data_license", "market_data_plan", mode="before"
    )
    @classmethod
    def _blank_text_is_unset(cls, v: object) -> object:
        return None if v is None or not str(v).strip() else str(v).strip()

    @field_validator("market_data_provider")
    @classmethod
    def _registered_provider(cls, v: str | None) -> str | None:
        """A misspelt provider fails at startup instead of silently staying disabled."""
        if v is None:
            return None
        from quantlab.data.vendors.registry import get_vendor

        return get_vendor(v).name

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        """Checks every production process needs (API, worker, reaper, migrations).

        The API's own secrets, the JWT signing key and the metrics token, are checked by
        :meth:`require_api_secrets` when the API starts. Workers never sign or verify a
        token, so they are not given the key (#117).
        """
        if self.environment == "production":
            if not self.cookie_secure:
                raise ValueError("QUANTLAB_COOKIE_SECURE must be true in production")
            # Redis holds the job queue and the login rate limits: without AUTH, anything
            # that reaches it can fill the queue or reset the limits (#115).
            redis_password = urlsplit(self.redis_url).password or ""
            if len(redis_password) < 16 or redis_password == DEV_REDIS_PASSWORD:
                raise ValueError(
                    "QUANTLAB_REDIS_URL must carry a real Redis password (16+ characters) "
                    "in production: set QUANTLAB_REDIS_PASSWORD"
                )
            if self.market_data_provider and self.market_data_api_key is None:
                raise ValueError(
                    "QUANTLAB_MARKET_DATA_API_KEY must be set (non-blank) when "
                    "QUANTLAB_MARKET_DATA_PROVIDER is"
                )
        return self

    def require_api_secrets(self) -> None:
        """Refuse to serve the API in production without its own secrets (#117).

        Called first by ``create_app``. The same checks used to run in every process, so
        workers and the reaper needed the API's signing key, which they never use.
        """
        if self.environment != "production":
            return
        if self.jwt_secret is None:
            raise ValueError("QUANTLAB_JWT_SECRET must be set in production")
        secret = self.jwt_secret.get_secret_value()
        if len(secret) < 32:
            raise ValueError("QUANTLAB_JWT_SECRET must be at least 32 characters")
        if any(m in secret.lower() for m in ("insecure", "change-me", "changeme", "dev-only")):
            raise ValueError("QUANTLAB_JWT_SECRET is a placeholder; generate a real secret")
        if self.metrics_token is None or len(self.metrics_token.get_secret_value()) < 16:
            raise ValueError("QUANTLAB_METRICS_TOKEN (16+ characters) must be set in production")

    @property
    def signing_key(self) -> str:
        """The JWT signing key (a shared development key when none is configured).

        In production there is no fallback: a process without the key fails closed at its
        first token operation instead of signing with a development key.
        """
        if self.jwt_secret is None:
            if self.environment == "production":
                raise RuntimeError(
                    "QUANTLAB_JWT_SECRET is not set: only the API signs tokens (#117)"
                )
            self.jwt_secret = _shared_dev_secret(self.data_dir)
        return self.jwt_secret.get_secret_value()

    @property
    def datasets_dir(self) -> Path:
        return self.data_dir / "datasets"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"


@lru_cache
def get_settings() -> Settings:
    return Settings()
