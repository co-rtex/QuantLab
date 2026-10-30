"""Metrics, structured logging and request correlation."""

from __future__ import annotations

import contextvars
import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import structlog
from prometheus_client import Counter, Gauge, Histogram
from starlette.requests import Request
from starlette.responses import Response

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

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
JOBS_RUNNING = Gauge("quantlab_jobs_running", "Jobs currently executing in this process")
CACHE_EVENTS = Counter("quantlab_cache_events_total", "Cache lookups", ["cache", "result"])
DATASET_ROWS = Gauge("quantlab_dataset_rows", "Rows in the most recently ingested dataset version")
DATASET_FRESHNESS_DAYS = Gauge(
    "quantlab_dataset_freshness_days", "Days between newest bar and ingestion time (latest)"
)
INGEST_QUALITY = Counter("quantlab_ingest_quality_total", "Ingestion quality outcomes", ["status"])


def _add_request_id(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    rid = request_id_var.get()
    if rid:
        event.setdefault("request_id", rid)
    return event


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_request_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
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
        HTTP_SECONDS.labels(request.method, template, str(status)).observe(elapsed)
        if template != "/metrics":
            log.info(
                "http_request",
                method=request.method,
                route=template,
                status=status,
                duration_ms=round(elapsed * 1000, 2),
            )
        request_id_var.reset(token)
