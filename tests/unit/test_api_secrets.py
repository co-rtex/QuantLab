"""Only the API needs the JWT signing key (#117). The metrics token is needed by every
process that serves /metrics, the API and each worker (#165, tests/unit/test_worker_metrics.py),
and is checked when that process starts, not when its modules are imported."""

from __future__ import annotations

import pytest

from quantlab_api.config import Settings

# What a worker, reaper or migration task gets in production: no API secrets.
WORKER = {
    "environment": "production",
    "cookie_secure": True,
    "redis_url": "rediss://:" + "r" * 24 + "@redis:6379/0",
}
API = {**WORKER, "jwt_secret": "s" * 40, "metrics_token": "m" * 20}


def test_a_worker_or_reaper_starts_in_production_without_the_api_secrets(monkeypatch):
    s = Settings(_env_file=None, **WORKER)
    assert s.jwt_secret is None and s.metrics_token is None
    # The worker's and reaper's modules import and configure under those settings.
    from quantlab_api import config

    monkeypatch.setattr(config, "get_settings", lambda: s)
    import quantlab_api.cli
    import quantlab_api.tasks
    import quantlab_api.worker  # noqa: F401


def test_the_api_refuses_to_start_without_its_secrets(monkeypatch):
    from quantlab_api import main

    for missing in ("jwt_secret", "metrics_token"):
        s = Settings(_env_file=None, **{k: v for k, v in API.items() if k != missing})
        monkeypatch.setattr(main, "get_settings", lambda s=s: s)
        with pytest.raises(
            ValueError, match="JWT_SECRET" if missing == "jwt_secret" else "METRICS_TOKEN"
        ):
            main.create_app()


@pytest.mark.parametrize(
    ("secret", "message"),
    [("short", "at least 32"), ("insecure-change-me-" + "x" * 30, "placeholder")],
)
def test_the_api_still_refuses_weak_signing_keys(secret, message):
    s = Settings(_env_file=None, **{**API, "jwt_secret": secret})
    with pytest.raises(ValueError, match=message):
        s.require_api_secrets()


def test_a_complete_api_configuration_passes():
    Settings(_env_file=None, **API).require_api_secrets()


def test_without_a_key_production_fails_closed_instead_of_using_a_dev_key(tmp_path):
    s = Settings(_env_file=None, **WORKER, data_dir=tmp_path)
    with pytest.raises(RuntimeError, match="only the API signs tokens"):
        _ = s.signing_key
    assert not (tmp_path / ".jwt-dev-secret").exists()  # no development key was created


def test_development_still_generates_a_shared_key(tmp_path):
    s = Settings(_env_file=None, environment="development", data_dir=tmp_path)
    assert len(s.signing_key) >= 32
