"""``log.exception`` logs in both formats, without frame locals (#202, #8).

The console format (development, compose's default) raised TypeError instead: the
exception was rendered to a list for JSON, which the console renderer can't print. Every
handler that logged an exception then failed inside its own except block: the API's 500
path, the reaper, and a worker's failed metrics fold, which stopped the worker.
"""

from __future__ import annotations

import json

import pytest
import structlog

from quantlab_api.observability import configure_logging, log

SECRET = "frame-local-do-not-log"


def _fail() -> None:
    password = SECRET  # noqa: F841 - a frame local that must not be logged
    raise RuntimeError("hash backend down")


def _log_a_failure() -> None:
    # A fresh logger, so the configuration just made is the one used even where loggers
    # are cached (as they were): the module's `log` could still hold an earlier test's.
    try:
        _fail()
    except RuntimeError:
        structlog.get_logger("quantlab").exception("unhandled_error", path="/probe")


@pytest.mark.parametrize("as_json", [True, False], ids=["json", "console"])
def test_an_exception_is_logged_without_frame_locals(capsys, as_json: bool) -> None:
    configure_logging(json=as_json)
    _log_a_failure()
    out = capsys.readouterr().out
    assert "unhandled_error" in out
    assert "hash backend down" in out  # the exception itself is still reported
    assert "_fail" in out  # with its traceback
    assert SECRET not in out


def test_json_carries_the_exception_as_structured_frames(capsys) -> None:
    configure_logging(json=True)
    _log_a_failure()
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    frames = event["exception"][0]["frames"]
    assert frames and all("locals" not in f for f in frames)


def test_a_new_configuration_applies_to_a_logger_already_used(capsys) -> None:
    """No logger keeps the configuration it first logged with: one used before
    configure_logging (at import, say) would otherwise never get it, and tests would
    depend on which of them configured logging first."""
    configure_logging(json=True)
    log.info("first")
    assert json.loads(capsys.readouterr().out)["event"] == "first"
    configure_logging(json=False)
    log.info("second")
    out = capsys.readouterr().out
    assert "second" in out and not out.lstrip().startswith("{")
