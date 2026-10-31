"""Device cookies keep an account's owner signed in under a distributed lockout (#55).

Each failed login counts against the account (30 per minute at the default limit of 10),
so three real addresses could keep any account at 429, including for its owner. A browser
that signed in before presents a device cookie and is throttled per device instead. The
cookie only selects a throttling budget; it never authenticates.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from quantlab_api.config import get_settings
from tests.integration.conftest import register

VICTIM = {"email": "victim@example.com", "password": "Correct-horse-9"}
WRONG = {"email": "victim@example.com", "password": "wrong-pass-1"}
LOGIN = "/api/v1/auth/login"


@pytest.fixture(autouse=True)
def limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production-like limits (10 per address, 30 failures per account), counted in one
    window so a slow run cannot straddle a minute boundary."""
    from quantlab_api.jobs import redis_conn
    from quantlab_api.security import login_limiter

    monkeypatch.setattr(get_settings(), "rate_limit_login_per_minute", 10)
    monkeypatch.setattr(get_settings(), "trusted_proxies", [])
    monkeypatch.setattr(login_limiter, "_bucket", lambda key, window: f"ql:rl:{key}:pinned")
    monkeypatch.setattr(redis_conn(), "expire", lambda *a, **k: True)


def _client(ip: str) -> TestClient:
    from quantlab_api.main import app

    return TestClient(app, client=(ip, 4000))


def _owner_browser(client: Any) -> TestClient:
    """The victim's own browser: it signed in once and holds a device cookie."""
    register(client, VICTIM["email"], VICTIM["password"])
    browser = _client("198.51.100.10")
    r = browser.post(LOGIN, json=VICTIM)
    assert r.status_code == 200, r.text
    assert browser.cookies.get("quantlab_device")
    return browser


def _lock_the_account() -> list[int]:
    """Three attacker addresses, ten wrong passwords each: the account budget is spent."""
    codes = []
    for i in range(3):
        with _client(f"203.0.113.{i + 1}") as attacker:
            codes += [attacker.post(LOGIN, json=WRONG).status_code for _ in range(10)]
    return codes


def test_a_lockout_from_several_addresses_spares_a_device_that_signed_in_before(client):
    browser = _owner_browser(client)
    assert _lock_the_account() == [401] * 30
    with _client("203.0.113.9") as attacker:  # an attacker without the cookie
        assert attacker.post(LOGIN, json=VICTIM).status_code == 429
    r = browser.post(LOGIN, json=VICTIM)
    assert r.status_code == 200, r.text


def test_an_attacker_without_the_cookie_stays_limited(client):
    _owner_browser(client)
    _lock_the_account()
    with _client("203.0.113.50") as fresh:
        assert fresh.post(LOGIN, json=WRONG).status_code == 429
        assert fresh.post(LOGIN, json=VICTIM).status_code == 429


def test_a_cookie_for_another_account_is_ignored(client):
    browser = _owner_browser(client)
    register(client, "other@example.com", "Correct-horse-9")
    for i in range(3):
        with _client(f"203.0.113.{i + 1}") as attacker:
            for _ in range(10):
                attacker.post(LOGIN, json={"email": "other@example.com", "password": "x" * 10})
    r = browser.post(LOGIN, json={"email": "other@example.com", "password": "Correct-horse-9"})
    assert r.status_code == 429


@pytest.mark.parametrize("damage", ["signature", "payload", "garbage"])
def test_a_tampered_cookie_is_ignored(client, damage):
    browser = _owner_browser(client)
    _lock_the_account()
    value = browser.cookies["quantlab_device"]
    body, _, sig = value.partition(".")
    forged = {
        "signature": f"{body}.{sig[:-2]}AA",
        "payload": f"{body[:-2]}AA.{sig}",
        "garbage": "not-a-device-cookie",
    }[damage]
    browser.cookies.clear()
    browser.cookies.set("quantlab_device", forged, path="/api/v1/auth")
    assert browser.post(LOGIN, json=VICTIM).status_code == 429


def test_an_expired_cookie_is_ignored(client):
    import time

    from sqlalchemy import select

    from quantlab_api.db import SessionLocal
    from quantlab_api.models import User
    from quantlab_api.security import DEVICE_MAX_AGE, device_token

    browser = _owner_browser(client)
    _lock_the_account()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == VICTIM["email"]))
        assert user is not None
        old = device_token(user, "a" * 32, now=time.time() - DEVICE_MAX_AGE - 1)
        fresh = device_token(user, "b" * 32)
    browser.cookies.clear()
    browser.cookies.set("quantlab_device", old, path="/api/v1/auth")
    assert browser.post(LOGIN, json=VICTIM).status_code == 429
    # The same cookie, freshly issued, is honoured: only its age differs.
    browser.cookies.clear()
    browser.cookies.set("quantlab_device", fresh, path="/api/v1/auth")
    assert browser.post(LOGIN, json=VICTIM).status_code == 200


def test_logging_out_everywhere_revokes_every_device_cookie(client):
    browser = _owner_browser(client)
    device = browser.cookies["quantlab_device"]
    token = browser.post(LOGIN, json=VICTIM).json()["access_token"]
    out = browser.post(
        "/api/v1/auth/logout?everywhere=true", headers={"Authorization": f"Bearer {token}"}
    )
    assert out.status_code == 204
    _lock_the_account()
    browser.cookies.clear()
    browser.cookies.set("quantlab_device", device, path="/api/v1/auth")
    assert browser.post(LOGIN, json=VICTIM).status_code == 429


def test_ten_failures_on_one_device_remove_its_exemption(client):
    browser = _owner_browser(client)
    # The same device on two networks, so the per-address limit (10) is not what stops it.
    roaming = _client("198.51.100.11")
    roaming.cookies.set("quantlab_device", browser.cookies["quantlab_device"], path="/api/v1/auth")
    codes = [browser.post(LOGIN, json=WRONG).status_code for _ in range(5)]
    codes += [roaming.post(LOGIN, json=WRONG).status_code for _ in range(5)]
    assert codes == [401] * 10
    _lock_the_account()
    assert roaming.post(LOGIN, json=VICTIM).status_code == 429


def test_the_device_cookie_never_authenticates(client):
    browser = _owner_browser(client)
    device = browser.cookies["quantlab_device"]
    anonymous = _client("198.51.100.20")
    anonymous.cookies.set("quantlab_device", device, path="/api/v1")
    assert anonymous.get("/api/v1/auth/me").status_code == 401
    assert anonymous.get("/api/v1/workspaces").status_code == 401


def test_cookie_attributes(client, monkeypatch):
    register(client, VICTIM["email"], VICTIM["password"])
    for secure, name in ((False, "quantlab_device"), (True, "__Secure-quantlab_device")):
        monkeypatch.setattr(get_settings(), "cookie_secure", secure)
        r = _client("198.51.100.30").post(LOGIN, json=VICTIM)
        cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith(name + "="))
        attrs = {a.strip().lower() for a in cookie.split(";")[1:]}
        assert "httponly" in attrs and "samesite=strict" in attrs
        assert "path=/api/v1/auth" in attrs
        assert ("secure" in attrs) is secure
