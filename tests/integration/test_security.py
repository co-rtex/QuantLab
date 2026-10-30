"""Authentication and authorization regression tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt

from quantlab_api.config import get_settings
from tests.integration.conftest import register, workspace_id


def test_register_login_me_logout(client):
    h = register(client)
    me = client.get("/api/v1/auth/me", headers=h)
    assert me.status_code == 200
    assert "password_hash" not in me.json()
    r = client.post(
        "/api/v1/auth/login", json={"email": "ALICE@example.com", "password": "Correct-horse-9"}
    )
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=lax" in r.headers["set-cookie"].lower()
    # cookie session works without the Authorization header
    assert client.get("/api/v1/auth/me").status_code == 200
    client.cookies.clear()


def test_bad_credentials_and_duplicates(client):
    register(client)
    bad = client.post(
        "/api/v1/auth/login", json={"email": "alice@example.com", "password": "wrong-Password1"}
    )
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "invalid_credentials"
    unknown = client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong-Password1"}
    )
    assert unknown.status_code == 401
    assert unknown.json()["error"]["message"] == bad.json()["error"]["message"]
    dup = client.post(
        "/api/v1/auth/register",
        json={"email": "alice@example.com", "password": "Another-pass-1", "display_name": "x"},
    )
    assert dup.status_code == 409
    weak = client.post(
        "/api/v1/auth/register",
        json={"email": "b@example.com", "password": "alllowercase", "display_name": "b"},
    )
    assert weak.status_code == 422


def test_unauthenticated_and_tampered_tokens(client):
    assert client.get("/api/v1/workspaces").status_code == 401
    h = register(client)
    token = h["Authorization"].split()[1]
    claims = jwt.decode(token, options={"verify_signature": False})
    forged = jwt.encode(claims, "not-the-secret", algorithm="HS256")
    assert (
        client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code
        == 401
    )
    none_alg = jwt.encode(claims, key="", algorithm="none")
    assert (
        client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {none_alg}"}).status_code
        == 401
    )
    expired_claims = dict(claims, exp=datetime.now(UTC) - timedelta(seconds=5))
    expired = jwt.encode(
        expired_claims, get_settings().jwt_secret.get_secret_value(), algorithm="HS256"
    )
    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "token_expired"


def test_logout_everywhere_revokes_tokens(client):
    h = register(client)
    assert client.post("/api/v1/auth/logout?everywhere=true", headers=h).status_code == 204
    assert client.get("/api/v1/auth/me", headers=h).status_code == 401


def test_roles_enforced(client):
    owner = register(client, "owner@example.com")
    viewer = register(client, "viewer@example.com")
    ws = workspace_id(client, owner)
    r = client.post(
        f"/api/v1/workspaces/{ws}/members",
        headers=owner,
        json={"email": "viewer@example.com", "role": "viewer"},
    )
    assert r.status_code == 201
    assert client.get(f"/api/v1/workspaces/{ws}", headers=viewer).json()["role"] == "viewer"
    create = client.post(
        f"/api/v1/workspaces/{ws}/strategies",
        headers=viewer,
        json={"name": "x", "strategy_key": "equal_weight"},
    )
    assert create.status_code == 403
    assert client.get(f"/api/v1/workspaces/{ws}/audit", headers=viewer).status_code == 403
    assert client.delete(f"/api/v1/workspaces/{ws}", headers=viewer).status_code == 403
    me = client.get("/api/v1/auth/me", headers=owner).json()
    last = client.delete(f"/api/v1/workspaces/{ws}/members/{me['id']}", headers=owner)
    assert last.status_code == 409


def test_login_rate_limit(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_login_per_minute", 3)
    codes = [
        client.post(
            "/api/v1/auth/login", json={"email": "x@example.com", "password": "Whatever-1"}
        ).status_code
        for _ in range(5)
    ]
    assert codes[:3] == [401, 401, 401]
    assert codes[3:] == [429, 429]


def test_sql_injection_like_inputs_are_inert(client, seeded):
    h = seeded["headers"]
    r = client.get(
        f"/api/v1/workspaces/{seeded['ws']}/experiments?q=%27%3B%20DROP%20TABLE%20users%3B--",
        headers=h,
    )
    assert r.status_code == 200
    assert client.get("/api/v1/auth/me", headers=h).status_code == 200
    bad_sym = client.get(f"/api/v1/dataset-versions/{seeded['dv']}/bars/..%2F..%2Fetc", headers=h)
    assert bad_sym.status_code in (404, 422)


def test_security_headers(client):
    r = client.get("/api/v1/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_production_settings_guard():
    import pytest

    from quantlab_api.config import Settings

    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(environment="production", cookie_secure=True)
    with pytest.raises(ValueError, match="placeholder"):
        Settings(
            environment="production",
            cookie_secure=True,
            metrics_token="m" * 20,
            jwt_secret="dev-only-insecure-secret-change-me-0123456789",
        )
    with pytest.raises(ValueError, match="COOKIE_SECURE"):
        Settings(
            environment="production",
            jwt_secret="x" * 40,
            cookie_secure=False,
            metrics_token="m" * 20,
        )


def test_account_throttle_counts_failures_only_and_spares_real_user(client, monkeypatch):
    """Failed guesses from many addresses throttle the account without touching others."""
    monkeypatch.setattr(get_settings(), "rate_limit_login_per_minute", 2)
    from quantlab_api.security import login_limiter

    register(client, "victim@example.com", "Correct-horse-9")
    login_limiter.reset()
    import fakeredis

    from quantlab_api.jobs import set_redis

    set_redis(fakeredis.FakeRedis())
    # Six failures from distinct addresses reach the account threshold (3 x limit).
    for i in range(6):
        login_limiter.check(f"login:ip:attacker-{i}", 1000)
        assert login_limiter.reserve("login:fail:victim@example.com", 1000)
    r = client.post(
        "/api/v1/auth/login", json={"email": "victim@example.com", "password": "Correct-horse-9"}
    )
    assert r.status_code == 429
    other = client.post(
        "/api/v1/auth/login", json={"email": "someone@example.com", "password": "Whatever-1"}
    )
    assert other.status_code == 401  # other accounts unaffected


def test_successful_logins_do_not_lock_account(client, monkeypatch):
    register(client, "user@example.com", "Correct-horse-9")
    monkeypatch.setattr(get_settings(), "rate_limit_login_per_minute", 1000)
    for _ in range(10):
        r = client.post(
            "/api/v1/auth/login", json={"email": "user@example.com", "password": "Correct-horse-9"}
        )
        assert r.status_code == 200
    from quantlab_api.security import login_limiter

    assert login_limiter.count("login:fail:user@example.com") == 0


def test_metrics_token_protects_endpoint(client, monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(get_settings(), "metrics_token", SecretStr("scrape-token-123"))
    from fastapi.testclient import TestClient

    from quantlab_api.main import create_app

    with TestClient(create_app()) as c:
        assert c.get("/metrics").status_code == 401
        ok = c.get("/metrics", headers={"Authorization": "Bearer scrape-token-123"})
        assert ok.status_code == 200


def test_synthetic_dataset_size_is_bounded(client):
    h = register(client)
    ws = workspace_id(client, h)
    r = client.post(
        f"/api/v1/workspaces/{ws}/datasets/synthetic",
        headers=h,
        json={
            "slug": "huge",
            "name": "Huge",
            "n_assets": 2000,
            "start": "1990-01-02",
            "end": "2019-12-31",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "dataset_too_large"


def test_fallback_limiter_is_bounded_and_atomic_reserve():
    from quantlab_api.jobs import set_redis
    from quantlab_api.security import RateLimiter

    class Down:
        def __getattr__(self, name):
            raise ConnectionError("down")

    set_redis(Down())  # type: ignore[arg-type]
    lim = RateLimiter()
    assert [lim.reserve("victim", 2) for _ in range(3)] == [True, True, False]
    for i in range(20_000):  # a key flood cannot evict or reset the victim's counter
        lim.reserve(f"k{i}", 10**9)
    assert lim.reserve("victim", 2) is False
    assert len(lim._counts) == RateLimiter.SHARDS  # constant memory
    lim.refund("victim")  # ignored in degraded mode (shards may be shared)
    assert lim.reserve("victim", 2) is False


def test_blank_secrets_are_treated_as_unset():
    import pytest

    from quantlab_api.config import Settings

    assert Settings(metrics_token="   ").metrics_token is None
    assert Settings(anthropic_api_key="").anthropic_api_key is None
    with pytest.raises(ValueError, match="METRICS_TOKEN"):
        Settings(
            environment="production", cookie_secure=True, metrics_token="", jwt_secret="k" * 40
        )
