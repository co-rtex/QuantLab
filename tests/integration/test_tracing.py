"""A research job's spans join the trace of the HTTP request that submitted it."""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.integration.conftest import run_pending_jobs


def test_request_and_job_share_one_trace(seeded, monkeypatch):
    from fastapi.testclient import TestClient
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    from quantlab_api import tracing
    from quantlab_api.main import create_app

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", trace.Once())
    trace.set_tracer_provider(provider)
    monkeypatch.setattr(tracing, "tracer", provider.get_tracer("quantlab"))
    from quantlab_api import jobs

    monkeypatch.setattr(jobs, "tracer", provider.get_tracer("quantlab"))
    app = create_app()
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    with TestClient(app) as c:
        r = c.post(
            f"/api/v1/workspaces/{seeded['ws']}/experiments",
            headers=seeded["headers"],
            json={
                "name": "traced",
                "dataset_version_id": seeded["dv"],
                "strategy_key": "equal_weight",
                "config": {"start": "2021-01-04"},
            },
        )
        assert r.status_code == 202
    assert run_pending_jobs() == ["completed"]
    spans = exporter.get_finished_spans()
    http = [s for s in spans if s.attributes.get("http.route", "").endswith("/experiments")]
    job = [s for s in spans if s.name == "job.execute"]
    handler = [s for s in spans if s.name == "job.handler"]
    assert http
    assert job
    assert handler
    trace_id = http[0].context.trace_id
    assert job[0].context.trace_id == trace_id
    assert handler[0].parent.span_id == job[0].context.span_id
    assert job[0].attributes["quantlab.job.outcome"] == "completed"
    FastAPIInstrumentor.uninstrument_app(app)
