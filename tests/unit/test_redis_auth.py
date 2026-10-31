"""Production refuses to start against a Redis without a real password (#115)."""

from __future__ import annotations

import pytest

from quantlab_api.config import Settings

PRODUCTION = {
    "environment": "production",
    "cookie_secure": True,
    "jwt_secret": "k" * 40,
    "metrics_token": "m" * 20,
}


@pytest.mark.parametrize(
    "url",
    [
        "redis://redis:6379/0",  # no AUTH
        "redis://:quantlab-dev@redis:6379/0",  # the development default
        "redis://:short@redis:6379/0",
        "redis://user@redis:6379/0",  # a user, no password
    ],
)
def test_production_refuses_redis_without_a_real_password(url: str) -> None:
    with pytest.raises(ValueError, match="Redis password"):
        Settings(**PRODUCTION, redis_url=url)


def test_production_accepts_a_real_redis_password() -> None:
    s = Settings(**PRODUCTION, redis_url="redis://:0123456789abcdef0123@redis:6379/0")
    assert s.environment == "production"


def test_development_needs_no_redis_password() -> None:
    assert Settings(environment="development", redis_url="redis://localhost:6380/0").redis_url
