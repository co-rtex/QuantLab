"""Client addresses behind the web tier and the TLS edge (#3, ADR-0008)."""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import anyio
import pytest

from quantlab_api.config import get_settings
from tests.integration.conftest import register


def _request(peer: str, xff: str | None = None):  # type: ignore[no-untyped-def]
    from starlette.requests import Request

    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "client": (peer, 1234), "headers": headers})


class FakeDNS:
    """Stands in for getaddrinfo: per-name answers, optional delay, records callers."""

    def __init__(self, answers: dict[str, str | None], delay: float = 0.0) -> None:
        self.answers = answers
        self.delay = delay
        self.threads: list[str] = []

    def __call__(self, host: str, *args: Any, **kwargs: Any) -> list[Any]:
        self.threads.append(threading.current_thread().name)
        time.sleep(self.delay)
        ip = self.answers.get(host)
        if ip is None:
            raise socket.gaierror(socket.EAI_NONAME, "unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


@pytest.fixture
def proxies(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A fresh resolver with short timings, installed as the module's instance."""
    from quantlab_api import security

    tp = security.TrustedProxies()
    tp.RETRY = 0.05
    monkeypatch.setattr(security, "trusted_proxies", tp)
    monkeypatch.setattr(get_settings(), "trusted_proxies", ["web"])
    return tp


def _wait_for(cond: Any, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_dns_is_never_resolved_on_the_event_loop(proxies, monkeypatch):
    from quantlab_api import security

    dns = FakeDNS({"web": "10.9.0.2"}, delay=2.0)
    monkeypatch.setattr(socket, "getaddrinfo", dns)

    async def from_async_route() -> tuple[str, float]:
        t0 = time.monotonic()
        ip = security.client_ip(_request("10.9.0.2", "198.51.100.7"))
        return ip, time.monotonic() - t0

    ip, took = anyio.run(from_async_route)
    assert took < 0.5, f"client_ip blocked the event loop for {took:.2f}s"
    assert ip == "10.9.0.2"  # not resolved yet: the peer is not trusted
    assert "MainThread" not in dns.threads
    # Once the background resolution lands, the same request is attributed correctly.
    assert _wait_for(lambda: bool(proxies._resolved))
    assert security.client_ip(_request("10.9.0.2", "198.51.100.7")) == "198.51.100.7"


def test_cold_start_in_a_worker_thread_resolves_inline(proxies, monkeypatch):
    """Sync routes (login, register) run in worker threads, where waiting is harmless,
    so the very first login after a restart is not attributed to the proxy."""
    from quantlab_api import security

    monkeypatch.setattr(socket, "getaddrinfo", FakeDNS({"web": "10.9.0.2"}))
    assert security.rate_limit_address(_request("10.9.0.2", "198.51.100.7")) == "198.51.100.7"


def test_failed_lookup_keeps_the_last_good_addresses(proxies, monkeypatch):
    from quantlab_api import security

    dns = FakeDNS({"web": "10.9.0.2"})
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    assert proxies.networks()  # configures and resolves "web"
    dns.answers["web"] = None  # DNS outage, or the container is being recreated
    assert not proxies.resolve_now()
    # Before #3 a failed refresh emptied the list for 30 s: everyone shared one bucket.
    assert security.rate_limit_address(_request("10.9.0.2", "198.51.100.7")) == "198.51.100.7"


def test_recreated_proxy_is_trusted_again_quickly(proxies, monkeypatch):
    from quantlab_api import security

    dns = FakeDNS({"web": "10.9.0.2"})
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    assert security.rate_limit_address(_request("10.9.0.2", "198.51.100.7")) == "198.51.100.7"
    dns.answers["web"] = "10.9.0.3"  # web recreated with a new address
    first = security.rate_limit_address(_request("10.9.0.3", "198.51.100.7"))
    assert first == "10.9.0.3"  # untrusted until re-resolved; the miss wakes the resolver
    assert _wait_for(
        lambda: security.rate_limit_address(_request("10.9.0.3", "198.51.100.7")) == "198.51.100.7",
        timeout=2.0,
    )


def test_rate_limit_address_modes(proxies, monkeypatch):
    from quantlab_api import security

    monkeypatch.setattr(socket, "getaddrinfo", FakeDNS({"web": "10.9.0.2"}))
    addr = security.rate_limit_address
    # A direct client is throttled on its socket address; its own header is ignored.
    assert addr(_request("203.0.113.5", "198.51.100.7")) == "203.0.113.5"
    # Behind the edge: the single address the edge wrote.
    assert addr(_request("10.9.0.2", "198.51.100.7")) == "198.51.100.7"
    # Behind the web tier without an edge it forwards nothing: unknown, not the proxy.
    assert addr(_request("10.9.0.2")) is None
    assert addr(_request("10.9.0.2", "garbage")) is None
    assert security.client_ip(_request("10.9.0.2")) == "10.9.0.2"  # audit still records


def test_without_an_edge_one_client_cannot_lock_everyone_out(monkeypatch):
    """No-edge mode: the web tier forwards no address. The per-address limit is skipped
    rather than applied to the web container's address, which every user shares."""
    from fastapi.testclient import TestClient

    from quantlab_api.main import app

    monkeypatch.setattr(get_settings(), "trusted_proxies", ["10.0.0.5"])
    monkeypatch.setattr(get_settings(), "rate_limit_login_per_minute", 3)
    with TestClient(app, client=("10.0.0.5", 4000)) as web:
        register(web, "victim@example.com", "Correct-horse-9")
        codes = [
            web.post(
                "/api/v1/auth/login",
                json={"email": f"x{i}@example.com", "password": "wrong-pass-1"},
            ).status_code
            for i in range(12)
        ]
        assert codes == [401] * 12  # different accounts: no shared bucket to exhaust
        ok = web.post(
            "/api/v1/auth/login",
            json={"email": "victim@example.com", "password": "Correct-horse-9"},
        )
        assert ok.status_code == 200, ok.text
        # The account limit (3 x per-minute limit) still bounds guessing on one account.
        guesses = [
            web.post(
                "/api/v1/auth/login",
                json={"email": "victim@example.com", "password": "wrong-pass-1"},
            ).status_code
            for _ in range(10)
        ]
        assert guesses[:9] == [401] * 9 and guesses[9] == 429
        # Registration is not throttled per address in this mode either.
        for i in range(5):
            register(web, f"new{i}@example.com")


def test_spoofed_header_from_a_direct_client_is_ignored(monkeypatch):
    """A client that reaches the API without a trusted proxy cannot pick a bucket."""
    from fastapi.testclient import TestClient

    from quantlab_api.main import app

    monkeypatch.setattr(get_settings(), "trusted_proxies", ["10.0.0.5"])
    monkeypatch.setattr(get_settings(), "rate_limit_login_per_minute", 3)
    with TestClient(app, client=("203.0.113.66", 4000)) as attacker:
        codes = [
            attacker.post(
                "/api/v1/auth/login",
                json={"email": f"x{i}@example.com", "password": "wrong-pass-1"},
                headers={"X-Forwarded-For": f"198.51.100.{i}"},
            ).status_code
            for i in range(5)
        ]
    assert codes == [401, 401, 401, 429, 429]  # rotating fake addresses does not help


def test_production_refuses_requests_without_a_client_address(monkeypatch):
    """Production behind a proxy that states no address (edge missing) fails closed."""
    from fastapi.testclient import TestClient

    from quantlab_api.main import app

    monkeypatch.setattr(get_settings(), "trusted_proxies", ["10.0.0.5"])
    with TestClient(app, client=("10.0.0.5", 4000)) as web:
        register(web, "user@example.com", "Correct-horse-9")
        monkeypatch.setattr(get_settings(), "environment", "production")
        creds = {"email": "user@example.com", "password": "Correct-horse-9"}
        login = web.post("/api/v1/auth/login", json=creds)
        reg = web.post(
            "/api/v1/auth/register",
            json={"email": "n@example.com", "password": "Correct-horse-9", "display_name": "n"},
        )
        assert (login.status_code, reg.status_code) == (503, 503)
        assert login.json()["error"]["code"] == "edge_required"
        # With the edge's address the same deployment works.
        ok = web.post(
            "/api/v1/auth/login", json=creds, headers={"X-Forwarded-For": "198.51.100.20"}
        )
        assert ok.status_code == 200, ok.text


def test_development_caps_anonymous_registrations_without_locking_logins(monkeypatch):
    from fastapi.testclient import TestClient

    from quantlab_api.main import app

    monkeypatch.setattr(get_settings(), "trusted_proxies", ["10.0.0.5"])
    monkeypatch.setattr(get_settings(), "rate_limit_login_per_minute", 1)
    with TestClient(app, client=("10.0.0.5", 4000)) as web:
        for i in range(10):
            register(web, f"r{i}@example.com")
        extra = web.post(
            "/api/v1/auth/register",
            json={"email": "r10@example.com", "password": "Correct-horse-9", "display_name": "r"},
        )
        assert extra.status_code == 429
        ok = web.post(
            "/api/v1/auth/login", json={"email": "r0@example.com", "password": "Correct-horse-9"}
        )
        assert ok.status_code == 200, ok.text
