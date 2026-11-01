"""The stack test may only restart a worktree slot's containers, never the main checkout's
(#182 review). A bare ``docker compose`` in a worktree targets the project ``quantlab``
when ``COMPOSE_PROJECT_NAME`` is not exported: the main checkout's stack and volumes."""

from __future__ import annotations

import pytest

from tests.stack import test_reproduce_across_restart as stack


@pytest.mark.parametrize("name", [None, "", "quantlab", " quantlab "])
def test_the_main_project_or_none_is_refused(monkeypatch, name):
    if name is None:
        monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    else:
        monkeypatch.setenv("COMPOSE_PROJECT_NAME", name)
    with pytest.raises(RuntimeError, match="refusing to restart the main checkout"):
        stack.compose_project()


def test_the_restart_names_the_slot_project_explicitly(monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "quantlab-agent-3-data")
    calls = []
    monkeypatch.setattr(stack.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    stack._restart()
    assert calls == [
        ["docker", "compose", "-p", "quantlab-agent-3-data", "restart", "api", "worker"]
    ]


def test_a_restart_without_a_slot_runs_nothing(monkeypatch):
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    calls = []
    monkeypatch.setattr(stack.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    with pytest.raises(RuntimeError):
        stack._restart()
    assert calls == []
