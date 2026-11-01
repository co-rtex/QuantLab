"""The reaper exports what it records, behind the API's token (#179)."""

from __future__ import annotations

import io
import uuid
from typing import Any

import pytest

from quantlab_api.config import Settings
from quantlab_api.observability import JOB_OUTCOMES, JOB_RETRIES, reaper_metrics_app

TOKEN = "r" * 24


def call(app: Any, auth: str | None = None) -> tuple[str, str]:
    environ = {"PATH_INFO": "/metrics", "REQUEST_METHOD": "GET", "wsgi.input": io.BytesIO()}
    if auth is not None:
        environ["HTTP_AUTHORIZATION"] = auth
    got: dict[str, str] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        got["status"] = status

    body = b"".join(app(environ, start_response)).decode()
    return got["status"], body


def test_the_reaper_serves_its_retries_and_outcomes_behind_the_token() -> None:
    kind = f"t.{uuid.uuid4().hex[:8]}"
    JOB_RETRIES.labels(kind, "lease_expired").inc()
    JOB_OUTCOMES.labels(kind, "failed").inc()
    app = reaper_metrics_app(TOKEN)
    assert call(app)[0].startswith("401")
    assert call(app, "Bearer wrong")[0].startswith("401")
    status, body = call(app, f"Bearer {TOKEN}")
    assert status.startswith("200")
    assert f'quantlab_job_retries_total{{kind="{kind}",reason="lease_expired"}} 1.0' in body
    assert f'quantlab_job_outcomes_total{{kind="{kind}",outcome="failed"}} 1.0' in body
    assert "quantlab_db_query_duration_seconds" in body


def test_the_reaper_serves_only_what_it_records() -> None:
    """Not the rest of the registry at zero: an extra quantlab_jobs_running 0 or
    quantlab_dataset_rows 0 series per reaper would show in every panel without an
    aggregation."""
    _, body = call(reaper_metrics_app(None))
    names = {
        line.split("{")[0].split(" ")[0]
        for line in body.splitlines()
        if line and not line.startswith("#")
    }
    families = {n.removesuffix("_total").removesuffix("_created") for n in names}
    families = {
        n.rsplit("_", 1)[0] if n.endswith(("_bucket", "_count", "_sum")) else n for n in families
    }
    assert families <= {
        "quantlab_job_retries",
        "quantlab_job_outcomes",
        "quantlab_db_query_duration_seconds",
    }
    assert "quantlab_jobs_running" not in body and "quantlab_dataset_rows" not in body


def production(**overrides: Any) -> Settings:
    return Settings(
        environment="production",
        cookie_secure=True,
        redis_url=f"redis://:{'r' * 20}@localhost:6379/0",
        **overrides,
    )


def run_reaper(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, port: str | None
) -> dict[str, Any]:
    """quantlab-cli reaper up to its first pass: whether it served, then stop."""
    from quantlab_api import cli, config, jobs, observability

    got: dict[str, Any] = {"served": None, "passes": 0}
    if port is None:
        monkeypatch.delenv("QUANTLAB_REAPER_METRICS_PORT", raising=False)
    else:
        monkeypatch.setenv("QUANTLAB_REAPER_METRICS_PORT", port)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(
        observability, "serve_reaper_metrics", lambda p, token: got.update(served=(p, token))
    )

    def one_pass(*_: object) -> None:
        got["passes"] += 1
        raise SystemExit  # out of the loop after the first pass

    monkeypatch.setattr(jobs, "reap", one_pass)
    with pytest.raises(SystemExit):
        cli._reaper(15.0)
    return got


@pytest.mark.parametrize("token", [None, "s" * 15])
def test_a_production_reaper_that_serves_refuses_without_a_real_token(
    monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    from quantlab_api import cli, config

    monkeypatch.setenv("QUANTLAB_REAPER_METRICS_PORT", "9102")
    monkeypatch.setattr(config, "get_settings", lambda: production(metrics_token=token))
    with pytest.raises(ValueError, match="QUANTLAB_METRICS_TOKEN"):
        cli._reaper(15.0)


def test_a_production_reaper_that_does_not_serve_needs_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Terraform ECS reaper: production, no metrics port, no token. It must start."""
    got = run_reaper(monkeypatch, production(metrics_token=None), None)
    assert got["served"] is None and got["passes"] == 1


def test_a_production_reaper_with_the_token_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    got = run_reaper(monkeypatch, production(metrics_token="s" * 16), "9102")
    assert got["served"] == (9102, "s" * 16) and got["passes"] == 1
