"""Runtime configuration from environment variables (prefix ``QUANTLAB_``)."""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SECRET = secrets.token_urlsafe(48)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QUANTLAB_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://quantlab:quantlab@localhost:5433/quantlab"
    redis_url: str = "redis://localhost:6380/0"
    data_dir: Path = Path("var/data")
    # In development a random per-process secret is used; production must set one.
    jwt_secret: SecretStr = SecretStr(_DEV_SECRET)
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
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-sonnet-5-5"
    git_sha: str = "unknown"

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.environment == "production":
            if self.jwt_secret.get_secret_value() == _DEV_SECRET:
                raise ValueError("QUANTLAB_JWT_SECRET must be set in production")
            if len(self.jwt_secret.get_secret_value()) < 32:
                raise ValueError("QUANTLAB_JWT_SECRET must be at least 32 characters")
            if not self.cookie_secure:
                raise ValueError("QUANTLAB_COOKIE_SECURE must be true in production")
        return self

    @property
    def datasets_dir(self) -> Path:
        return self.data_dir / "datasets"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"


@lru_cache
def get_settings() -> Settings:
    return Settings()
