"""Authentication and authorization regression tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt

from quantlab_api.config import get_settings
from tests.integration.conftest import join_workspace, register, workspace_id


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
    expired = jwt.encode(expired_claims, get_settings().signing_key, algorithm="HS256")
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
    join_workspace(client, owner, ws, viewer, "viewer@example.com", "viewer")
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

    # The API's secrets are checked when the API starts (#117), the rest for every process.
    redis = "redis://:" + "r" * 20 + "@redis:6379/0"
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(
            environment="production", cookie_secure=True, redis_url=redis
        ).require_api_secrets()
    with pytest.raises(ValueError, match="placeholder"):
        Settings(
            environment="production",
            cookie_secure=True,
            redis_url=redis,
            metrics_token="m" * 20,
            jwt_secret="dev-only-insecure-secret-change-me-0123456789",
        ).require_api_secrets()
    with pytest.raises(ValueError, match="COOKIE_SECURE"):
        Settings(
            environment="production",
            jwt_secret="x" * 40,
            cookie_secure=False,
            metrics_token="m" * 20,
        )


def _request(peer: str, xff: str | None = None):  # type: ignore[no-untyped-def]
    from starlette.requests import Request

    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "client": (peer, 1234), "headers": headers})


def test_client_address_is_only_taken_from_trusted_proxies(monkeypatch):
    from quantlab_api import security

    monkeypatch.setattr(get_settings(), "trusted_proxies", [])
    assert security.client_ip(_request("10.0.0.5", "1.2.3.4")) == "10.0.0.5"
    monkeypatch.setattr(get_settings(), "trusted_proxies", ["10.0.0.0/24", "localhost"])
    # The client prepended a fake hop; only the hop the proxy observed is used.
    assert security.client_ip(_request("10.0.0.5", "6.6.6.6, 1.2.3.4")) == "1.2.3.4"
    assert security.client_ip(_request("127.0.0.1", "1.2.3.4, 10.0.0.9")) == "1.2.3.4"
    assert security.client_ip(_request("9.9.9.9", "1.2.3.4")) == "9.9.9.9"  # untrusted peer
    assert security.client_ip(_request("10.0.0.5", "not-an-ip")) == "10.0.0.5"
    assert security.client_ip(_request("10.0.0.5")) == "10.0.0.5"


def test_failed_logins_behind_the_proxy_do_not_lock_out_other_users(monkeypatch):
    """Before the fix the API saw every browser as the web container, so one client's ten
    failed logins returned 429 to every other user's correct password."""
    from fastapi.testclient import TestClient

    from quantlab_api.main import app

    monkeypatch.setattr(get_settings(), "trusted_proxies", ["10.0.0.5"])
    monkeypatch.setattr(get_settings(), "rate_limit_login_per_minute", 3)
    with TestClient(app, client=("10.0.0.5", 4000)) as web:
        register(web, "victim@example.com", "Correct-horse-9")
        attacker = {"X-Forwarded-For": "198.51.100.66"}
        codes = [
            web.post(
                "/api/v1/auth/login",
                json={"email": f"x{i}@example.com", "password": "wrong-pass-1"},
                headers=attacker,
            ).status_code
            for i in range(5)
        ]
        assert codes[-1] == 429
        r = web.post(
            "/api/v1/auth/login",
            json={"email": "victim@example.com", "password": "Correct-horse-9"},
            headers={"X-Forwarded-For": "203.0.113.10"},
        )
        assert r.status_code == 200, r.text
    from sqlalchemy import select

    from quantlab_api.db import SessionLocal
    from quantlab_api.models import AuditEvent

    with SessionLocal() as db:
        ips = set(db.scalars(select(AuditEvent.ip).where(AuditEvent.action == "user.login")))
    assert ips == {"203.0.113.10"}


def _dev_key(data_dir: str) -> str:
    from quantlab_api.config import Settings

    return Settings(environment="development", data_dir=data_dir).signing_key


def test_unset_dev_secret_is_shared_by_every_worker_process(tmp_path):
    """Without QUANTLAB_JWT_SECRET, `uvicorn --workers 4` used to give each worker its own
    random key, so ~3 in 4 requests failed with 401. Processes started together must agree
    on one key, and a restart must keep it (nobody gets signed out)."""
    import multiprocessing as mp

    with mp.get_context("spawn").Pool(8) as pool:
        keys = pool.map(_dev_key, [str(tmp_path)] * 16)
    assert len(set(keys)) == 1 and len(keys[0]) >= 32
    assert _dev_key(str(tmp_path)) == keys[0]
    assert (tmp_path / ".jwt-dev-secret").stat().st_mode & 0o077 == 0
    # The key is written to a temporary file and renamed into place; none are left over.
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [".jwt-dev-secret", ".jwt-dev-secret.lock"]


def test_an_empty_dev_secret_file_from_a_crash_is_replaced(tmp_path):
    """The old writer created the file and then wrote it, so a crash in between left an
    empty file that stopped every later start ("is empty; delete it and restart")."""
    path = tmp_path / ".jwt-dev-secret"
    path.touch(mode=0o600)
    key = _dev_key(str(tmp_path))
    assert len(key) >= 32 and path.read_text().strip() == key


def test_workers_repairing_an_empty_dev_secret_together_agree_on_one_key(tmp_path):
    """#64 review: one process unlinked the empty file and created K1, then another's late
    unlink deleted K1 and created K2, so workers signed with different keys. The repair
    now happens under an exclusive lock."""
    import multiprocessing as mp

    (tmp_path / ".jwt-dev-secret").touch(mode=0o600)
    with mp.get_context("spawn").Pool(8) as pool:
        keys = pool.map(_dev_key, [str(tmp_path)] * 16)
    assert len(set(keys)) == 1 and len(keys[0]) >= 32


def test_the_dev_secret_does_not_need_hard_links(tmp_path, monkeypatch):
    """#64 review: os.link fails with EPERM or ENOTSUP on some filesystems."""
    import errno
    import os

    def no_links(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "hard links not supported")

    monkeypatch.setattr(os, "link", no_links)
    key = _dev_key(str(tmp_path))
    assert len(key) >= 32 and _dev_key(str(tmp_path)) == key


def test_a_dev_secret_readable_by_others_is_refused(tmp_path):
    import pytest

    path = tmp_path / ".jwt-dev-secret"
    path.write_text("k" * 64)
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="readable by other users"):
        _dev_key(str(tmp_path))


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
    # Constant memory: one fixed table per window length, however many keys.
    assert [len(counts) for counts, _ in lim._tables.values()] == [RateLimiter.SHARDS]
    lim.refund("victim")  # ignored in degraded mode (shards may be shared)
    assert lim.reserve("victim", 2) is False


def test_fallback_limiter_keeps_windows_of_different_lengths_apart(monkeypatch):
    """#37 review: one-hour invitation limits and one-minute login limits shared shards.
    With Redis down, a colliding pair kept resetting each other's count: 100 of 100
    invitations passed a limit of 20, and login throttles were reset too."""
    from quantlab_api.jobs import set_redis
    from quantlab_api.security import RateLimiter

    class Down:
        def __getattr__(self, name):
            raise ConnectionError("down")

    set_redis(Down())  # type: ignore[arg-type]
    lim = RateLimiter()
    monkeypatch.setattr(lim, "_shard", lambda key: 7)  # every key collides
    invites, logins = [], []
    for _ in range(100):  # interleaved, as live traffic would be
        invites.append(lim.reserve("invite:user:a", 20, 3600))
        logins.append(lim.reserve("login:ip:b", 10, 60))
    assert invites.count(True) == 20
    assert logins.count(True) == 10
    # Same-window collisions still only make throttling stricter: they share one count.
    other = RateLimiter()
    monkeypatch.setattr(other, "_shard", lambda key: 7)
    both = [other.reserve(k, 5, 60) for _ in range(5) for k in ("x", "y")]
    assert both.count(True) == 5


def test_blank_secrets_are_treated_as_unset():
    import pytest

    from quantlab_api.config import Settings

    assert Settings(metrics_token="   ").metrics_token is None
    assert Settings(anthropic_api_key="").anthropic_api_key is None
    with pytest.raises(ValueError, match="METRICS_TOKEN"):
        Settings(
            environment="production",
            cookie_secure=True,
            redis_url="redis://:" + "r" * 20 + "@redis:6379/0",
            metrics_token="",
            jwt_secret="k" * 40,
        ).require_api_secrets()
