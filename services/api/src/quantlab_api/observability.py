"""Metrics, structured logging and request correlation."""

from __future__ import annotations

import contextlib
import contextvars
import glob
import hmac
import logging
import os
import socketserver
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import structlog
from prometheus_client import Counter, Gauge, Histogram
from starlette.requests import Request
from starlette.responses import Response
from structlog.tracebacks import ExceptionDictTransformer

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
HTTP_SECONDS = Histogram(
    "quantlab_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2.5, 5, 10),
)
DB_QUERY_SECONDS = Histogram(
    "quantlab_db_query_duration_seconds",
    "Database statement latency",
    ["verb"],
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1),
)
JOB_QUEUE_WAIT_SECONDS = Histogram(
    "quantlab_job_queue_wait_seconds",
    "Time from submission to first start",
    ["kind"],
    buckets=(0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 300),
)
JOB_RUN_SECONDS = Histogram(
    "quantlab_job_run_seconds",
    "Job execution time",
    ["kind", "outcome"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 900),
)
JOB_OUTCOMES = Counter("quantlab_job_outcomes_total", "Job terminal outcomes", ["kind", "outcome"])
JOB_RETRIES = Counter("quantlab_job_retries_total", "Job retries scheduled", ["kind", "reason"])
JOBS_RUNNING = Gauge(
    "quantlab_jobs_running", "Jobs currently executing", multiprocess_mode="livesum"
)
CACHE_EVENTS = Counter("quantlab_cache_events_total", "Cache lookups", ["cache", "result"])
# 1 while any live API process's rate limiter counts locally after a Redis error (#207).
# Labelled, so no process writes a sample (in multiprocess mode, a file) until a breaker
# opens: an unlabelled gauge would add one to every work horse's directory.
RATE_LIMITER_BREAKER_OPEN = Gauge(
    "quantlab_rate_limiter_breaker_open",
    "1 while the rate limiter skips Redis after an error and counts in the process",
    ["limiter"],
    multiprocess_mode="livemax",
)
DATASET_ROWS = Gauge(
    "quantlab_dataset_rows",
    "Rows in the most recently ingested dataset version",
    multiprocess_mode="mostrecent",
)
DATASET_FRESHNESS_DAYS = Gauge(
    "quantlab_dataset_freshness_days",
    "Days between newest bar and ingestion time (latest)",
    multiprocess_mode="mostrecent",
)
INGEST_QUALITY = Counter("quantlab_ingest_quality_total", "Ingestion quality outcomes", ["status"])


def _add_request_id(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    rid = request_id_var.get()
    if rid:
        event.setdefault("request_id", rid)
    from quantlab_api.tracing import trace_id_hex

    tid = trace_id_hex()
    if tid:
        event.setdefault("trace_id", tid)
    return event


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    # Tracebacks without frame locals, in both formats: structlog's defaults (dict_tracebacks,
    # and the console's rich tracebacks) log them, so any logged 500 printed request bodies
    # such as passwords (#8). The exception is rendered to frames for JSON only: the console
    # renderer prints a traceback itself, and given those frames it raised TypeError instead,
    # inside the handler that was logging (#202).
    renderer: list[Any] = (
        [
            structlog.processors.ExceptionRenderer(ExceptionDictTransformer(show_locals=False)),
            structlog.processors.JSONRenderer(),
        ]
        if json
        else [structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback)]
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_request_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            *renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        # Not cached: a cached logger keeps the configuration it first logged with, so one
        # used before this call never got it, and which call came first decided the format.
        cache_logger_on_first_use=False,
    )


log = structlog.get_logger("quantlab")


def _valid_request_id(value: str | None) -> bool:
    return (
        bool(value)
        and len(value or "") <= 64
        and all(c.isalnum() or c in "-_." for c in value or "")
    )


async def request_context_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    incoming = request.headers.get("x-request-id")
    rid = incoming if incoming and _valid_request_id(incoming) else uuid.uuid4().hex
    token = request_id_var.set(rid)
    request.state.request_id = rid
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        elapsed = time.perf_counter() - start
        route = request.scope.get("route")
        template = getattr(route, "path", "unmatched")
        # The method is client-controlled; an open label set let anyone mint new series.
        method = request.method if request.method in KNOWN_METHODS else "OTHER"
        HTTP_SECONDS.labels(method, template, str(status)).observe(elapsed)
        if template != "/metrics":
            log.info(
                "http_request",
                method=request.method,
                route=template,
                status=status,
                duration_ms=round(elapsed * 1000, 2),
            )
        request_id_var.reset(token)


# ------------------------------------------------------------------- worker metrics (#165)
# Job metrics are recorded in RQ's forked work horses, so a worker's own registry never
# sees them: each worker container keeps a multiprocess directory of its own (pids are
# unique within it, and every container's worker is pid 1, so the directory is never
# shared with another container or the API) and serves it from the worker process.

WSGIApp = Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]

# Held by a scrape and by a compaction, so no scrape sees a dead process's samples both in
# its own file and in the archive, or in neither.
_FILES = threading.Lock()

Sample = tuple[float, float]  # value, timestamp
# How a dead process's samples join the archive, as MultiProcessCollector combines them
# across files. Gauges in "all" and "live*" modes are per process, so they are not
# archived (mark_process_dead removes the live ones).
_ADD: Callable[[Sample, Sample], Sample] = lambda a, b: (a[0] + b[0], max(a[1], b[1]))  # noqa: E731
_ARCHIVED: dict[str, Callable[[Sample, Sample], Sample]] = {
    "counter": _ADD,
    "histogram": _ADD,
    "gauge_sum": _ADD,
    "gauge_max": lambda a, b: max(a, b, key=lambda s: s[0]),
    "gauge_min": lambda a, b: min(a, b, key=lambda s: s[0]),
    "gauge_mostrecent": lambda a, b: b if b[1] > a[1] else a,
}


def compact_dead_process(pid: int, directory: str | None = None) -> None:
    """Fold a dead process's files into one archive file per kind (#165).

    Every work horse is a new pid, and multiprocess mode keeps a file per kind per pid,
    so a worker accumulated about three files (20 KiB of tmpfs, charged to its memory
    limit) per job, and each scrape read them all. Counters and histograms are added to
    the archive and so never go down; scrapes are held off while it happens. All or
    nothing: a failure before the renames leaves every file as it was.
    """
    d = directory or os.environ["PROMETHEUS_MULTIPROC_DIR"]
    with _FILES:
        # 1. Every merge, in memory: nothing on disk changes yet.
        merged: dict[str, tuple[str, dict[str, Sample]]] = {}
        for kind, merge in _ARCHIVED.items():
            dead = os.path.join(d, f"{kind}_{pid}.db")
            if not os.path.exists(dead):
                continue
            archive = os.path.join(d, f"{kind}_archive.db")
            have = _read(archive) if os.path.exists(archive) else {}
            for key, sample in _read(dead).items():
                have[key] = merge(have[key], sample) if key in have else sample
            merged[kind] = (dead, have)
        # 2. Each new archive complete in a temporary file the collector does not read (it
        # reads *.db). An error here leaves the archives and the dead files as they were,
        # so nothing is counted twice or lost; the dead files stay counted on their own.
        staged: list[tuple[str, str, str]] = []
        tmp = ""
        try:
            for kind, (dead, have) in merged.items():
                tmp = os.path.join(d, f"{kind}_archive.db.tmp")
                _write(tmp, have)
                staged.append((tmp, os.path.join(d, f"{kind}_archive.db"), dead))
        except BaseException:
            for leftover in {t for t, _, _ in staged} | ({tmp} if tmp else set()):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(leftover)
            raise
        # 3. Swap each archive in and drop its dead file, kind by kind: two renames on the
        # same filesystem, with scrapes held off.
        for tmp, archive, dead in staged:
            os.replace(tmp, archive)
            os.remove(dead)


def _read(path: str) -> dict[str, Sample]:
    from prometheus_client.mmap_dict import MmapedDict

    values = MmapedDict.read_all_values_from_file(path)  # type: ignore[no-untyped-call]
    return {key: (value, ts) for key, value, ts, _ in values}


def _write(path: str, samples: dict[str, Sample]) -> None:
    from prometheus_client.mmap_dict import MmapedDict

    with contextlib.suppress(FileNotFoundError):
        os.remove(path)  # a leftover from a fold that failed before its rename
    out = MmapedDict(path)  # type: ignore[no-untyped-call]
    try:
        for key, (value, ts) in samples.items():
            out.write_value(key, value, ts)  # type: ignore[no-untyped-call]
    finally:
        out.close()  # type: ignore[no-untyped-call]


def _has_header(path: str) -> bool:
    try:
        return os.path.getsize(path) >= 8  # MmapedDict's used-length header and padding
    except FileNotFoundError:
        return False


def _file_order(path: str) -> tuple[int, int, str]:
    """Archives first, then each process's files by pid (see :func:`worker_collector`)."""
    owner = os.path.basename(path)[: -len(".db")].rsplit("_", 1)[-1]
    if owner == "archive":
        return (0, 0, path)
    return (1, int(owner), path) if owner.isdigit() else (2, 0, path)


def worker_collector() -> type:
    """MultiProcessCollector as a worker's endpoint needs it (#165).

    * It skips a file still being created. A new work horse opens each of its files empty
      and then sizes it, and a scrape that listed one in between failed reading 0 bytes
      (``unpack_from requires a buffer of at least 4 bytes``); every job is a new horse.
      Such a file holds nothing yet, and once it has its header it reads correctly.
    * It adds files in a fixed order, archives first and then by pid. Floating-point sums
      depend on order, so the directory's own order made float series (a histogram's
      ``_sum``) wobble in the last bit, and Prometheus reads any drop in a counter as a
      reset. In this order a fold adds to the archive exactly the term the scrape added
      next, so a scrape gives the same bits before and after it (while pids increase).
    """
    from prometheus_client.multiprocess import MultiProcessCollector

    class WorkerCollector(MultiProcessCollector):
        def collect(self) -> Any:
            files = sorted(glob.glob(os.path.join(self._path, "*.db")), key=_file_order)
            return self.merge([f for f in files if _has_header(f)], accumulate=True)  # type: ignore[no-untyped-call]

    return WorkerCollector


def worker_metrics_app(token: str | None) -> WSGIApp:
    """``GET /metrics`` with this container's samples, behind the API's bearer token.

    ``token`` None (development) serves without one, as the API does.
    """
    from prometheus_client import CollectorRegistry

    registry = CollectorRegistry()
    worker_collector()(registry)
    return _metrics_app(token, registry)


# What the reaper records (#179): lease-loss retries, the outcomes it decides, and its DB
# statements. Its endpoint serves only these, not the zero-valued rest of the registry
# (an extra quantlab_dataset_rows 0 series, for one).
REAPER_METRICS = (JOB_RETRIES, JOB_OUTCOMES, DB_QUERY_SECONDS)


def reaper_metrics_app(token: str | None) -> WSGIApp:
    """``GET /metrics`` for the reaper, behind the API's bearer token (#179).

    The reaper is one process that never forks, so its samples are in its own registry: no
    multiprocess directory is needed. Its counters start from zero when it restarts, which
    Prometheus reads as a reset, as for any process.
    """
    from prometheus_client import REGISTRY, CollectorRegistry

    names = {next(iter(m.describe())).name for m in REAPER_METRICS}

    class Only:
        def collect(self) -> Iterable[Any]:
            return (family for family in REGISTRY.collect() if family.name in names)

    registry = CollectorRegistry(auto_describe=False)
    registry.register(Only())
    return _metrics_app(token, registry)


def _metrics_app(token: str | None, registry: Any) -> WSGIApp:
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    def app(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        if environ.get("PATH_INFO") != "/metrics" or environ.get("REQUEST_METHOD") != "GET":
            start_response("404 Not Found", [("Content-Length", "0")])
            return [b""]
        if token is not None:
            given = environ.get("HTTP_AUTHORIZATION", "").removeprefix("Bearer ").strip()
            if not token or not hmac.compare_digest(given.encode(), token.encode()):
                start_response("401 Unauthorized", [("Content-Length", "0")])
                return [b""]
        with _FILES:
            body = generate_latest(registry)
        start_response(
            "200 OK", [("Content-Type", CONTENT_TYPE_LATEST), ("Content-Length", str(len(body)))]
        )
        return [body]

    return app


class _QuietHandler(WSGIRequestHandler):
    # A connection that sends nothing is dropped after this long instead of holding its
    # thread (any container on the network can open one, before any token is checked).
    timeout = 10

    def log_message(self, format: str, *args: Any) -> None:
        return None  # one line per scrape on stderr is noise


class _ThreadingServer(socketserver.ThreadingMixIn, WSGIServer):
    """One thread per connection, so an idle or slow client cannot hold up a scrape."""

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that timed out or went away is not an error worth a traceback on stderr.
        if isinstance(sys.exc_info()[1], TimeoutError | ConnectionError):
            return
        super().handle_error(request, client_address)


def serve_worker_metrics(port: int, token: str | None) -> WSGIServer:
    """Serve :func:`worker_metrics_app` from a daemon thread of the worker process."""
    return _serve(port, worker_metrics_app(token))


def serve_reaper_metrics(port: int, token: str | None) -> WSGIServer:
    """Serve :func:`reaper_metrics_app` from a daemon thread of the reaper (#179)."""
    return _serve(port, reaper_metrics_app(token))


def _serve(port: int, app: WSGIApp) -> WSGIServer:
    """All interfaces inside the container: Prometheus reaches it over the compose network,
    and the port is never published to the host."""
    server = make_server(
        "0.0.0.0",  # noqa: S104
        port,
        app,
        server_class=_ThreadingServer,
        handler_class=_QuietHandler,
    )
    threading.Thread(target=server.serve_forever, name="metrics", daemon=True).start()
    return server
