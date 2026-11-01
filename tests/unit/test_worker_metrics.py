"""Each worker serves its job metrics, behind the API's token (#165)."""

from __future__ import annotations

import io
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import fakeredis
import pytest
from rq import Queue

from quantlab_api.config import Settings
from quantlab_api.jobs import RQ_OPTIONS
from quantlab_api.observability import worker_metrics_app

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t" * 24


def call(app: Any, path: str = "/metrics", auth: str | None = None, method: str = "GET"):
    environ = {"PATH_INFO": path, "REQUEST_METHOD": method, "wsgi.input": io.BytesIO()}
    if auth is not None:
        environ["HTTP_AUTHORIZATION"] = auth
    got: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        got["status"], got["headers"] = status, dict(headers)

    body = b"".join(app(environ, start_response))
    return got["status"], got["headers"], body


@pytest.fixture
def multiproc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    return tmp_path


def test_the_worker_endpoint_requires_the_token(multiproc: Path) -> None:
    app = worker_metrics_app(TOKEN)
    assert call(app)[0].startswith("401")
    assert call(app, auth="Bearer wrong")[0].startswith("401")
    assert call(app, auth=f"Bearer {TOKEN[:-1]}")[0].startswith("401")
    # As the API's /metrics: an optional "Bearer " prefix, then the token compared exactly.
    assert call(app, auth=TOKEN)[0].startswith("200")
    status, headers, _ = call(app, auth=f"Bearer {TOKEN}")
    assert status.startswith("200") and headers["Content-Type"].startswith("text/plain")


def test_the_worker_endpoint_serves_only_get_metrics(multiproc: Path) -> None:
    app = worker_metrics_app(TOKEN)
    assert call(app, "/", auth=f"Bearer {TOKEN}")[0].startswith("404")
    assert call(app, method="POST", auth=f"Bearer {TOKEN}")[0].startswith("404")


def test_without_a_token_development_serves_openly_as_the_api_does(multiproc: Path) -> None:
    assert call(worker_metrics_app(None))[0].startswith("200")


def production(**overrides: Any) -> Settings:
    return Settings(
        environment="production",
        cookie_secure=True,
        redis_url=f"redis://:{'r' * 20}@localhost:6379/0",
        **overrides,
    )


@pytest.mark.parametrize("token", [None, "s" * 15])
def test_a_production_worker_refuses_to_start_without_a_real_metrics_token(
    monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    from quantlab_api import worker

    built: list[object] = []
    monkeypatch.setattr(worker, "get_settings", lambda: production(metrics_token=token))
    monkeypatch.setattr(worker, "QuantlabWorker", lambda *a, **k: built.append(k))
    monkeypatch.setattr(worker, "setup_tracing", lambda *a, **k: None)
    with pytest.raises(ValueError, match="QUANTLAB_METRICS_TOKEN"):
        worker.main()
    assert built == []  # refused before it took any work


def test_a_production_worker_with_the_token_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    from quantlab_api import worker

    built: dict[str, object] = {}

    class Recorder:
        def __init__(self, queues: object, **kwargs: object) -> None:
            built.update(kwargs)

        def work(self, **_: object) -> None:
            return None

    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    monkeypatch.setattr(worker, "get_settings", lambda: production(metrics_token="s" * 16))
    monkeypatch.setattr(worker, "QuantlabWorker", Recorder)
    monkeypatch.setattr(worker, "redis_conn", fakeredis.FakeRedis)
    monkeypatch.setattr(
        worker, "get_queue", lambda: Queue("q", connection=fakeredis.FakeRedis(), **RQ_OPTIONS)
    )  # type: ignore[arg-type]
    monkeypatch.setattr(worker, "setup_tracing", lambda *a, **k: None)
    worker.main()
    assert "name" in built


@pytest.mark.parametrize("token", [None, "s" * 15])
def test_the_api_refuses_under_the_same_rule(token: str | None) -> None:
    s = production(metrics_token=token, jwt_secret="j" * 40)
    with pytest.raises(ValueError, match="QUANTLAB_METRICS_TOKEN"):
        s.require_api_secrets()
    with pytest.raises(ValueError, match="QUANTLAB_METRICS_TOKEN"):
        s.require_metrics_token()


def test_outside_production_no_token_is_needed() -> None:
    Settings(environment="development", metrics_token=None).require_metrics_token()


def horses(tmp_path: Path, jobs: int, mode: str) -> dict[str, Any]:
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path), "PYTHONPATH": str(ROOT)}
    out = subprocess.run(
        [sys.executable, "-m", "tests.unit.metrics_compaction_child", str(jobs), mode],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(out.stdout)


def test_compaction_keeps_the_directory_bounded_and_every_sample_exact(tmp_path: Path) -> None:
    jobs = 40
    got = horses(tmp_path, jobs, "compact")
    # The worker's own two files and one archive per kind, however many jobs ran.
    assert got["files"] == [
        "counter_archive.db",
        "gauge_livesum_" + got["files"][1].split("_")[2],
        "gauge_mostrecent_" + got["files"][1].split("_")[2],
        "gauge_mostrecent_archive.db",
        "histogram_archive.db",
    ]
    assert got["after_each"] == [float(i) for i in range(1, jobs + 1)]  # nothing lost or doubled
    assert got["run_count"] == jobs and got["run_sum"] == 2.0 * jobs and got["le_2"] == jobs
    assert got["rows"] == 1000 + jobs - 1  # the most recent horse's value
    assert got["running"] == 0


def test_a_scrape_during_compaction_never_sees_a_counter_go_down(tmp_path: Path) -> None:
    got = horses(tmp_path, 60, "compact")
    seen = got["seen"]
    assert len(seen) > 10, "the concurrent scraper barely ran"
    assert all(b >= a for a, b in itertools.pairwise(seen)), "a counter went down"
    assert max(seen) <= 60


def test_without_compaction_every_job_leaves_files(tmp_path: Path) -> None:
    """The control: what the archive saves (three files per job)."""
    got = horses(tmp_path, 40, "keep")
    assert len(got["files"]) == 2 + 3 * 40
    assert got["after_each"] == [float(i) for i in range(1, 41)]


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker compose CLI")
def test_each_worker_start_gets_an_empty_multiprocess_directory() -> None:
    """A tmpfs at the directory: Docker keeps a restarted container's writable layer, so
    a directory there would hand the new pid 1 its predecessor's gauge files (#165)."""
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
    out = subprocess.run(
        ["docker", "compose", "--profile", "app", "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    worker = json.loads(out)["services"]["worker"]
    directory = worker["environment"]["PROMETHEUS_MULTIPROC_DIR"]
    mounts = dict(m.partition(":")[::2] for m in worker["tmpfs"])
    assert directory in mounts
    # Writable by the image's user: a default tmpfs is root's, and the worker then failed
    # at import with PermissionError on its first metric file (found by the live probe).
    uid = re.search(r"useradd [^\n]*--uid (\d+)", (ROOT / "services/api/Dockerfile").read_text())
    assert uid is not None
    options = dict(o.split("=", 1) for o in mounts[directory].split(","))
    assert options["uid"] == uid.group(1) and options["gid"] == uid.group(1)
