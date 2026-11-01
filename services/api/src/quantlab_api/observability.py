"""Metrics, structured logging and request correlation."""

from __future__ import annotations

import contextvars
import hmac
import logging
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
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_request_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            # Frames without their local variables: structlog's default (dict_tracebacks)
            # logs them, so any logged 500 printed request bodies such as passwords.
            structlog.processors.ExceptionRenderer(ExceptionDictTransformer(show_locals=False)),
            structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        cache_logger_on_first_use=True,
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


def worker_metrics_app(token: str | None) -> WSGIApp:
    """``GET /metrics`` with this container's samples, behind the API's bearer token.

    ``token`` None (development) serves without one, as the API does.
    """
    from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
    from prometheus_client.multiprocess import MultiProcessCollector

    registry = CollectorRegistry()
    MultiProcessCollector(registry)  # type: ignore[no-untyped-call]

    def app(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        if environ.get("PATH_INFO") != "/metrics" or environ.get("REQUEST_METHOD") != "GET":
            start_response("404 Not Found", [("Content-Length", "0")])
            return [b""]
        if token is not None:
            given = environ.get("HTTP_AUTHORIZATION", "").removeprefix("Bearer ").strip()
            if not token or not hmac.compare_digest(given.encode(), token.encode()):
                start_response("401 Unauthorized", [("Content-Length", "0")])
                return [b""]
        body = generate_latest(registry)
        start_response(
            "200 OK", [("Content-Type", CONTENT_TYPE_LATEST), ("Content-Length", str(len(body)))]
        )
        return [body]

    return app


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return None  # one line per scrape on stderr is noise


def serve_worker_metrics(port: int, token: str | None) -> WSGIServer:
    """Serve :func:`worker_metrics_app` from a daemon thread of the worker process.

    All interfaces inside the container: Prometheus reaches each replica over the compose
    network, and the port is never published to the host.
    """
    app = worker_metrics_app(token)
    server = make_server("0.0.0.0", port, app, handler_class=_QuietHandler)  # noqa: S104
    threading.Thread(target=server.serve_forever, name="metrics", daemon=True).start()
    return server
