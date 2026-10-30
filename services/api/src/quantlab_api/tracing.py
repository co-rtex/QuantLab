"""OpenTelemetry tracing.

Tracing is configured from environment variables:

* ``QUANTLAB_OTEL_ENDPOINT`` - OTLP/HTTP traces endpoint (e.g. ``http://jaeger:4318/v1/traces``)
* ``QUANTLAB_OTEL_CONSOLE=1`` - print spans to stdout (debugging)

Without either, the global tracer is a no-op. HTTP requests are instrumented, and the
submitting request's trace context is stored on each job so the worker's spans join the
same trace: one trace covers request -> queue -> execution -> commit.
"""

from __future__ import annotations

import os
from typing import Any

from opentelemetry import context, propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)

_configured = False
tracer = trace.get_tracer("quantlab")


def setup_tracing(service: str, synchronous: bool = False) -> bool:
    """Configure the global tracer provider.

    ``synchronous`` exports each span as it ends. Workers use it because RQ runs every job
    in a short-lived forked process, where a batching export thread does not survive.
    """
    global _configured
    if _configured:
        return True
    endpoint = os.environ.get("QUANTLAB_OTEL_ENDPOINT")
    console = os.environ.get("QUANTLAB_OTEL_CONSOLE") == "1"
    if not endpoint and not console:
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    processor = SimpleSpanProcessor if synchronous else BatchSpanProcessor
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(processor(OTLPSpanExporter(endpoint=endpoint)))
    if console:
        provider.add_span_processor(processor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True
    return True


def current_carrier() -> dict[str, str]:
    """W3C trace context of the active span, for storing alongside a job."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def attach_carrier(carrier: dict[str, Any] | None) -> object | None:
    if not carrier:
        return None
    return context.attach(propagate.extract(carrier))


def detach(token: object | None) -> None:
    if token is not None:
        context.detach(token)  # type: ignore[arg-type]


def trace_id_hex() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return f"{ctx.trace_id:032x}" if ctx.is_valid else None


def flush(timeout_ms: int = 5000) -> None:
    """Export buffered spans now. RQ runs each job in a forked work horse that exits right
    after the job, before a batch processor would flush on its own."""
    provider = trace.get_tracer_provider()
    force = getattr(provider, "force_flush", None)
    if force is not None:
        force(timeout_ms)
