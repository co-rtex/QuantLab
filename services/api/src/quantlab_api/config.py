"""Runtime configuration from environment variables (prefix ``QUANTLAB_``)."""

from __future__ import annotations

import os
import secrets
import time
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET_FILE = ".jwt-dev-secret"  # noqa: S105 - a file name, not a secret


def _shared_dev_secret(data_dir: Path) -> SecretStr:
    """A development signing key shared by every process using ``data_dir``.

    A random per-process key breaks as soon as the API runs more than one worker
    (``uvicorn --workers 4``): a token signed by one worker is rejected by the others, so
    roughly three requests in four fail. The first process creates the file atomically
    (O_EXCL, mode 0600); the others, and later restarts, read it.
    """
    path = data_dir / DEV_SECRET_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        for _ in range(50):  # another process may be between create and write
            value = path.read_text().strip()
            if value:
                return SecretStr(value)
            time.sleep(0.02)
        raise RuntimeError(f"{path} is empty; delete it and restart") from None
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_urlsafe(48))
    return SecretStr(path.read_text().strip())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QUANTLAB_", env_file=".env", extra="ignore")

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

    @field_validator("metrics_token", "anthropic_api_key", "jwt_secret", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        """Compose passes unset variables as empty strings; treat those as not configured."""
        if v is None:
            return None
        raw = v.get_secret_value() if isinstance(v, SecretStr) else str(v)
        return None if not raw.strip() else v

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.environment == "production":
            if self.jwt_secret is None:
                raise ValueError("QUANTLAB_JWT_SECRET must be set in production")
            secret = self.jwt_secret.get_secret_value()
            if len(secret) < 32:
                raise ValueError("QUANTLAB_JWT_SECRET must be at least 32 characters")
            if any(m in secret.lower() for m in ("insecure", "change-me", "changeme", "dev-only")):
                raise ValueError("QUANTLAB_JWT_SECRET is a placeholder; generate a real secret")
            if self.metrics_token is None or len(self.metrics_token.get_secret_value()) < 16:
                raise ValueError(
                    "QUANTLAB_METRICS_TOKEN (16+ characters) must be set in production"
                )
            if not self.cookie_secure:
                raise ValueError("QUANTLAB_COOKIE_SECURE must be true in production")
        return self

    @property
    def signing_key(self) -> str:
        """The JWT signing key (a shared development key when none is configured)."""
        if self.jwt_secret is None:
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
