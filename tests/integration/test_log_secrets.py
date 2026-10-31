"""Logged tracebacks must not carry frame locals (#8).

structlog's ``dict_tracebacks`` renders exceptions with ``show_locals=True`` by default, so
any 500 logged with ``log.exception`` printed the variables of every frame: request
bodies with passwords, tokens and secrets.
"""

from __future__ import annotations

import json

from tests.integration.conftest import register

SECRET = "Correct-horse-9-do-not-log"


def test_unhandled_error_logs_no_frame_locals(monkeypatch, capsys):
    from fastapi.testclient import TestClient

    from quantlab_api.main import app
    from quantlab_api.routes import auth

    client = TestClient(app, raise_server_exceptions=False)
    register(client, "user@example.com", SECRET)

    def boom(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("hash backend down")

    monkeypatch.setattr(auth, "verify_password", boom)
    capsys.readouterr()
    r = client.post("/api/v1/auth/login", json={"email": "user@example.com", "password": SECRET})
    assert r.status_code == 500
    out = capsys.readouterr()
    logged = out.out + out.err
    assert "unhandled_error" in logged
    assert "hash backend down" in logged  # the exception itself is still reported
    assert SECRET not in logged
    for line in logged.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        for exc in event.get("exception", []) or []:
            for frame in exc.get("frames", []):
                assert "locals" not in frame, frame
