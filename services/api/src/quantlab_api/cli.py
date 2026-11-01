"""Operational CLI: ``quantlab-cli <command>``."""

from __future__ import annotations

import argparse
import signal
import sys
import time

from quantlab_api.observability import configure_logging, log


def _reaper(interval: float) -> None:
    import os

    from quantlab_api import tasks  # noqa: F401
    from quantlab_api.config import get_settings
    from quantlab_api.jobs import reap
    from quantlab_api.observability import serve_reaper_metrics

    # Its retries and outcomes at :<port>/metrics when QUANTLAB_REAPER_METRICS_PORT is set
    # (compose sets it, #179). Serving, it follows the API's rule: no production reaper
    # without the metrics token. Without the port (the Terraform ECS task) it serves
    # nothing and needs no token.
    port = os.environ.get("QUANTLAB_REAPER_METRICS_PORT")
    if port:
        settings = get_settings()
        settings.require_metrics_token()
        token = settings.metrics_token.get_secret_value() if settings.metrics_token else None
        serve_reaper_metrics(int(port), token)
        log.info("reaper_metrics", port=int(port))

    stop = False

    def _stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("reaper_start", interval=interval)
    while not stop:
        try:
            reap()
        except Exception as exc:  # keep the loop alive across transient DB/Redis errors
            log.error("reaper_error", error=str(exc))
        for _ in range(int(interval * 10)):
            if stop:
                break
            time.sleep(0.1)


def _run_job(job_id: str) -> None:
    from quantlab_api import tasks  # noqa: F401
    from quantlab_api.jobs import execute_job

    print(execute_job(job_id))


def main(argv: list[str] | None = None) -> None:
    configure_logging(json=False)
    p = argparse.ArgumentParser(prog="quantlab-cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reaper", help="recover stale jobs periodically")
    r.add_argument("--interval", type=float, default=15.0)
    j = sub.add_parser("run-job", help="execute one job synchronously (debugging)")
    j.add_argument("job_id")
    args = p.parse_args(argv)
    if args.cmd == "reaper":
        _reaper(args.interval)
    elif args.cmd == "run-job":
        _run_job(args.job_id)
    else:  # pragma: no cover
        p.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
