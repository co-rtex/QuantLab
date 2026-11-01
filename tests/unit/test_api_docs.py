"""The API's interactive docs and schema are served only in development (#201).

Swagger UI is an HTML page that loads its script from a CDN and runs an inline one; through
the web proxy it ran on the app's own origin, outside the pages' Content-Security-Policy.
The web proxy refuses these paths in every environment (apps/web/e2e/security.spec.ts)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quantlab_api.config import Settings
from tests.unit.test_api_secrets import API

DOCS = ["/api/v1/docs", "/api/v1/openapi.json", "/docs/oauth2-redirect", "/api/v1/redoc"]


def _client(monkeypatch, tmp_path, environment: str) -> TestClient:
    from quantlab_api import main

    if environment == "production":
        s = Settings(_env_file=None, **API)
    else:
        s = Settings(_env_file=None, environment=environment, data_dir=tmp_path)
    monkeypatch.setattr(main, "get_settings", lambda: s)
    # Logging is configured process-wide: development's console format would outlive this.
    monkeypatch.setattr(main, "configure_logging", lambda **_: None)
    return TestClient(main.create_app())


@pytest.mark.parametrize("environment", ["production", "test"])
@pytest.mark.parametrize("path", DOCS)
def test_no_docs_outside_development(monkeypatch, tmp_path, environment: str, path: str) -> None:
    assert _client(monkeypatch, tmp_path, environment).get(path).status_code == 404


def test_the_schema_is_still_built_in_production(monkeypatch, tmp_path) -> None:
    app = _client(monkeypatch, tmp_path, "production").app
    assert "/api/v1/workspaces/{workspace_id}/experiments" in app.openapi()["paths"]


def test_development_serves_them(monkeypatch, tmp_path) -> None:
    c = _client(monkeypatch, tmp_path, "development")
    assert "swagger-ui" in c.get("/api/v1/docs").text
    assert "/api/v1/health" in c.get("/api/v1/openapi.json").json()["paths"]
    assert c.get("/docs/oauth2-redirect").status_code == 200
    assert c.get("/api/v1/redoc").status_code == 404  # never enabled
