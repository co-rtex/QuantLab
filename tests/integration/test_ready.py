"""/ready tells anonymous callers which dependency is down, not how (#8)."""

from __future__ import annotations


class _Down:
    def ping(self) -> None:
        raise ConnectionError("Error 61 connecting to redis-internal.example:6379.")


def test_ready_hides_exception_details_and_logs_them(client, capsys):
    from quantlab_api.jobs import set_redis

    set_redis(_Down())  # type: ignore[arg-type]
    capsys.readouterr()
    r = client.get("/api/v1/ready")
    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "checks": {"database": "ok", "redis": "error"}}
    assert "ConnectionError" not in r.text and "redis-internal" not in r.text
    logged = capsys.readouterr().out
    assert "ready_check_failed" in logged
    assert "ConnectionError" in logged and "redis-internal" in logged  # for operators


def test_ready_reports_ok_when_healthy(client):
    r = client.get("/api/v1/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}
