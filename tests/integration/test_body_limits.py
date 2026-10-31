"""Request body limits for bodies without Content-Length (#8)."""

from __future__ import annotations

from collections.abc import Iterator


def _chunks(total: int, size: int = 64 * 1024) -> Iterator[bytes]:
    sent = 0
    while sent < total:
        n = min(size, total - sent)
        sent += n
        yield b" " * n


def test_an_oversized_chunked_json_body_is_a_413_not_a_400(client):
    """The limit middleware stops reading at the limit, but FastAPI's JSON parsing caught
    that and answered 400 "error parsing the body": a chunked client never learnt that
    its body was too large."""
    r = client.post(
        "/api/v1/auth/login",
        content=_chunks(3 * 2**20),  # a generator: sent chunked, no Content-Length
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413, r.text
    assert r.json()["error"]["code"] == "payload_too_large"


def test_a_chunked_body_under_the_limit_is_still_parsed(client):
    import json

    body = json.dumps({"email": "nobody@example.com", "password": "wrong-pass-1"}).encode()
    r = client.post(
        "/api/v1/auth/login", content=iter([body]), headers={"content-type": "application/json"}
    )
    assert r.status_code == 401
