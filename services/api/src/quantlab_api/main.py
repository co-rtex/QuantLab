"""FastAPI application factory."""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from quantlab_api import tasks  # noqa: F401 - registers job handlers
from quantlab_api.config import get_settings
from quantlab_api.db import get_engine
from quantlab_api.errors import install_error_handlers
from quantlab_api.jobs import redis_conn
from quantlab_api.limits import BodySizeLimitMiddleware
from quantlab_api.observability import configure_logging, log, request_context_middleware
from quantlab_api.routes import (
    auth,
    datasets,
    experiments,
    jobs,
    ml,
    optimize,
    portfolios,
    reconciliation,
    research,
    strategies,
    workspaces,
)
from quantlab_api.tracing import setup_tracing

API_PREFIX = "/api/v1"


def _brief(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"


def create_app() -> FastAPI:
    settings = get_settings()
    settings.require_api_secrets()  # only the API holds the signing key (#117)
    configure_logging(json=settings.environment != "development")
    app = FastAPI(
        title="QuantLab API",
        version="0.1.0",
        description="Reproducible quantitative research platform. All market data served by "
        "a synthetic provider is labelled `is_synthetic=true`.",
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
    )
    install_error_handlers(app)
    if setup_tracing("quantlab-api"):
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="/metrics,/api/v1/health")
    app.add_middleware(BodySizeLimitMiddleware)
    app.middleware("http")(request_context_middleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def security_headers(request: Any, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    api = APIRouter(prefix=API_PREFIX)

    @api.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/ready", tags=["system"])
    def ready() -> JSONResponse:
        # Anonymous callers learn only which dependency is down, not how (the exception
        # names the driver and often the host); operators read the detail in the log.
        checks: dict[str, str] = {}
        try:
            with get_engine().connect() as c:
                c.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:
            checks["database"] = "error"
            log.warning("ready_check_failed", component="database", error=_brief(exc))
        try:
            redis_conn().ping()
            checks["redis"] = "ok"
        except Exception as exc:
            checks["redis"] = "error"
            log.warning("ready_check_failed", component="redis", error=_brief(exc))
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse(
            {"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503
        )

    @api.get("/version", tags=["system"])
    def version() -> dict[str, str]:
        from quantlab.backtest.engine import ENGINE_VERSION

        return {"api": app.version, "engine": ENGINE_VERSION, "git_sha": settings.git_sha}

    for r in (
        auth.router,
        workspaces.router,
        datasets.router,
        strategies.router,
        experiments.router,
        jobs.router,
        research.router,
        ml.router,
        portfolios.router,
        optimize.router,
        reconciliation.router,
    ):
        api.include_router(r)
    app.include_router(api)

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request) -> Response:
        token = settings.metrics_token
        if token is not None:
            expected = token.get_secret_value()
            given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not expected or not hmac.compare_digest(given, expected):
                return Response(status_code=401)
        if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
            # Several API processes: aggregate every process's samples per scrape.
            from prometheus_client import CollectorRegistry, multiprocess

            registry = CollectorRegistry()
            multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
            return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
