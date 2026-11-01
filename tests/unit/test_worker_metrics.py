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


def test_a_file_a_new_horse_is_still_creating_does_not_fail_a_scrape(multiproc: Path) -> None:
    """A horse opens each file empty, then sizes it; a scrape listing it in between read 0
    bytes and failed (found by the concurrent-scrape test under full-suite load)."""
    for kind in ("counter", "histogram", "gauge_livesum", "gauge_mostrecent"):
        (multiproc / f"{kind}_4242.db").touch()
    status, _, body = call(worker_metrics_app(None))
    assert status.startswith("200"), body


def test_without_a_token_development_serves_openly_as_the_api_does(multiproc: Path) -> None:
    assert call(worker_metrics_app(None))[0].startswith("200")


def production(**overrides: Any) -> Settings:
    return Settings(
        environment="production",
        cookie_secure=True,
        redis_url=f"redis://:{'r' * 20}@localhost:6379/0",
        **overrides,
    )


def start_worker(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, multiproc_dir: Path | None
) -> dict[str, Any]:
    """quantlab-worker's main(), up to taking work: what it built and whether it served."""
    from quantlab_api import worker

    got: dict[str, Any] = {"built": None, "served": None}

    class Recorder:
        def __init__(self, queues: object, **kwargs: object) -> None:
            got["built"] = kwargs

        def work(self, **_: object) -> None:
            return None

    if multiproc_dir is None:
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    else:
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(multiproc_dir))
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "QuantlabWorker", Recorder)
    monkeypatch.setattr(worker, "redis_conn", fakeredis.FakeRedis)
    monkeypatch.setattr(
        worker, "get_queue", lambda: Queue("q", connection=fakeredis.FakeRedis(), **RQ_OPTIONS)
    )  # type: ignore[arg-type]
    monkeypatch.setattr(worker, "setup_tracing", lambda *a, **k: None)
    monkeypatch.setattr(
        worker, "serve_worker_metrics", lambda port, token: got.update(served=token)
    )
    worker.main()
    return got


@pytest.mark.parametrize("token", [None, "s" * 15])
def test_a_production_worker_that_serves_metrics_refuses_without_a_real_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, token: str | None
) -> None:
    with pytest.raises(ValueError, match="QUANTLAB_METRICS_TOKEN"):
        start_worker(monkeypatch, production(metrics_token=token), tmp_path)


def test_a_production_worker_that_does_not_serve_metrics_needs_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Terraform ECS worker: production, no multiprocess directory, no metrics token
    (infra/terraform/ecs.tf gives it to the API only). It must start (#178 review)."""
    got = start_worker(monkeypatch, production(metrics_token=None), None)
    assert got["built"] is not None and got["served"] is None


def test_a_production_worker_with_the_token_starts_and_serves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    got = start_worker(monkeypatch, production(metrics_token="s" * 16), tmp_path)
    assert got["built"] is not None and got["served"] == "s" * 16


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
    """With each compaction slowed, a scrape that did not wait for it would count a dead
    horse twice (archive and its own file) and then once: up, then down; or fail, having
    listed a file the compaction removed before it was read."""
    got = horses(tmp_path, 30, "slow")
    seen = got["seen"]
    assert got["errors"] == []
    assert len(seen) > 10, "the concurrent scraper barely ran"
    assert seen[-1] == 30
    assert all(b >= a for a, b in itertools.pairwise(seen)), "a counter went down"
    assert max(seen) <= 30, "a horse was counted twice"


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


def test_compaction_changes_nothing_a_scrape_sees_for_every_archived_kind(tmp_path: Path) -> None:
    """Counter, histogram and gauges in sum, max, min and mostrecent modes, with quotes,
    backslashes, newlines and non-ASCII in labels: the scrape is identical after folding
    every dead horse (the review's 200-horse script, as a test)."""
    d = tmp_path / "d"
    d.mkdir()
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(d), "PYTHONPATH": str(ROOT)}
    out = subprocess.run(
        [sys.executable, "-m", "tests.unit.metrics_equivalence_child", "60"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    got = json.loads(out.stdout)
    assert got["compacted"] == got["plain"]
    for mode in ("sum", "max", "min", "mostrecent"):
        assert any(line.startswith(f"eq_gauge_{mode}{{") for line in got["plain"]), mode
    assert got["files_plain"] > 100
    # The parent's own two files and one archive per kind (counter, histogram, 4 gauges).
    assert len(got["files_compacted"]) <= 8, got["files_compacted"]


def _files(d: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(d.iterdir())}


def _scrape(d: Path) -> list[str]:
    from prometheus_client import CollectorRegistry, generate_latest
    from prometheus_client.multiprocess import MultiProcessCollector

    registry = CollectorRegistry()
    MultiProcessCollector(registry, path=str(d))  # type: ignore[no-untyped-call]
    return sorted(generate_latest(registry).decode().splitlines())


def _dead_horse(d: Path) -> None:
    """A dead horse's counter, histogram and mostrecent files, and an existing archive."""
    from prometheus_client.mmap_dict import MmapedDict, mmap_key

    def write(name: str, rows: list[tuple[str, str, dict[str, str], float, float]]) -> None:
        f = MmapedDict(str(d / name))  # type: ignore[no-untyped-call]
        for metric, sample, labels, value, ts in rows:
            key = mmap_key(metric, sample, list(labels), list(labels.values()), "help")
            f.write_value(key, value, ts)  # type: ignore[no-untyped-call]
        f.close()  # type: ignore[no-untyped-call]

    write("counter_archive.db", [("ev", "ev_total", {"k": "a"}, 5.0, 0.0)])
    write(
        "counter_7.db",
        [("ev", "ev_total", {"k": "a"}, 2.0, 0.0), ("ev", "ev_total", {"k": "b"}, 1.0, 0.0)],
    )
    write(
        "histogram_7.db",
        [
            ("hs", "hs_bucket", {"le": "1.0"}, 1.0, 0.0),
            ("hs", "hs_bucket", {"le": "+Inf"}, 1.0, 0.0),
            ("hs", "hs_sum", {}, 0.5, 0.0),
        ],
    )
    write("gauge_mostrecent_7.db", [("rows", "rows", {}, 42.0, 100.0)])


def test_a_fold_that_fails_mid_write_leaves_every_file_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's case: an OSError on the 2nd archive write. Before, the keys already
    merged were counted twice and the pid's other kinds were left behind."""
    from prometheus_client.mmap_dict import MmapedDict

    from quantlab_api.observability import compact_dead_process

    _dead_horse(tmp_path)
    before_files, before_scrape = _files(tmp_path), _scrape(tmp_path)
    real_write = MmapedDict.write_value
    calls = {"n": 0}

    def failing(self: Any, key: str, value: float, ts: float) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("injected: disk full")
        real_write(self, key, value, ts)

    monkeypatch.setattr(MmapedDict, "write_value", failing)
    with pytest.raises(OSError, match="injected"):
        compact_dead_process(7, str(tmp_path))
    assert _files(tmp_path) == before_files  # byte for byte, and no temporary file left
    assert _scrape(tmp_path) == before_scrape

    monkeypatch.setattr(MmapedDict, "write_value", real_write)
    compact_dead_process(7, str(tmp_path))  # the next fold succeeds
    assert _scrape(tmp_path) == before_scrape
    assert sorted(_files(tmp_path)) == [
        "counter_archive.db",
        "gauge_mostrecent_archive.db",
        "histogram_archive.db",
    ]


def _worker_after_a_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fold: Any) -> Any:
    from rq import Worker

    from quantlab_api import worker

    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    monkeypatch.setattr(Worker, "execute_job", lambda self, job, queue: None)  # the job ran
    monkeypatch.setattr(worker, "compact_dead_process", fold)
    conn = fakeredis.FakeRedis()
    w = worker.QuantlabWorker(
        [Queue("q", connection=conn, **RQ_OPTIONS)],  # type: ignore[arg-type]
        connection=conn,
        name="w",
        **RQ_OPTIONS,  # type: ignore[arg-type]
    )
    w._last_horse = 4242
    return w


def test_a_sigterm_during_the_fold_still_stops_the_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """rq raises StopRequested from its SIGTERM handler. Swallowed with the fold's other
    errors, the worker took a new job and was SIGKILLed in it 10 s later (#178 review)."""
    from rq.exceptions import StopRequested

    def interrupted(pid: int) -> None:
        raise StopRequested()

    w = _worker_after_a_job(monkeypatch, tmp_path, interrupted)
    with pytest.raises(StopRequested):
        w.execute_job(None, None)


def test_a_failing_fold_never_stops_the_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def broken(pid: int) -> None:
        raise OSError("disk full")

    w = _worker_after_a_job(monkeypatch, tmp_path, broken)
    w.execute_job(None, None)  # logged, not raised
    assert w._last_horse == 0


def test_every_worker_gauge_is_removed_on_death_or_archived() -> None:
    """A gauge in the default "all" mode is per process and neither removed nor archived:
    one would bring back a file per job. Pin the modes the worker's metrics use."""
    from prometheus_client import Gauge

    from quantlab_api import observability as O

    gauges = {name: m for name, m in vars(O).items() if isinstance(m, Gauge)}
    assert {"JOBS_RUNNING", "DATASET_ROWS", "DATASET_FRESHNESS_DAYS"} <= set(gauges)
    for name, g in gauges.items():
        mode = g._multiprocess_mode
        assert mode.startswith("live") or f"gauge_{mode}" in O._ARCHIVED, f"{name}: {mode}"


def test_an_idle_connection_does_not_block_a_scrape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any container on the network can open a connection and send nothing. The endpoint
    serves each on its own thread, and drops an idle one after the handler's timeout."""
    import socket
    import time
    import urllib.request

    from quantlab_api import observability as O

    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    # Long enough that a single-threaded server would visibly hold the scrape behind the
    # idle connection, short enough that the drop is seen within the test.
    monkeypatch.setattr(O._QuietHandler, "timeout", 3)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = O.serve_worker_metrics(port, None)
    try:
        idle = socket.create_connection(("127.0.0.1", port))
        t = time.monotonic()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as r:
            assert r.status == 200
        assert time.monotonic() - t < 1  # its own thread: not held behind the idle one
        idle.settimeout(6)
        assert idle.recv(1) == b""  # closed by the server after its timeout
        idle.close()
    finally:
        server.shutdown()
        server.server_close()
