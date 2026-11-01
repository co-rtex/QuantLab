"""RQ worker entrypoint: ``quantlab-worker``.

Workers are stateless; job state lives in PostgreSQL (see ``jobs.py``). Run several
replicas for parallelism; each executes one job at a time in a forked work horse so a
crashing or runaway job cannot take the worker down.
"""

from __future__ import annotations

import os
from typing import Any

from rq import Worker
from rq.exceptions import StopRequested

from quantlab_api import tasks  # noqa: F401 - registers job handlers
from quantlab_api.config import get_settings
from quantlab_api.jobs import RQ_OPTIONS, get_queue, horse_killed, redis_conn, worker_identity
from quantlab_api.observability import (
    compact_dead_process,
    configure_logging,
    log,
    serve_worker_metrics,
)
from quantlab_api.tracing import setup_tracing


class QuantlabWorker(Worker):
    """RQ's forking worker. Once a work horse has exited, however it ended (#165):

    * its live gauges (``quantlab_jobs_running``) are dropped: a horse killed mid-job never
      decrements them, and they would read 1 until the container restarts;
    * its other metric files are folded into the archive (``compact_dead_process``), so the
      directory and each scrape stay the same size however many jobs have run.
    """

    _last_horse: int = 0

    def fork_work_horse(self, job: Any, queue: Any) -> None:
        super().fork_work_horse(job, queue)  # the horse itself never returns (os._exit)
        # RQ resets horse_pid to 0 once it has reaped the horse, so keep it here.
        self._last_horse = self.horse_pid

    def execute_job(self, job: Any, queue: Any) -> None:
        try:
            super().execute_job(job, queue)
        finally:
            horse, self._last_horse = self._last_horse, 0
            if horse and os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
                from prometheus_client import multiprocess

                try:
                    multiprocess.mark_process_dead(horse)  # type: ignore[no-untyped-call]
                    compact_dead_process(horse)
                except StopRequested:
                    # A SIGTERM during the fold: rq's warm shutdown must still happen, or the
                    # worker would take a new job and be SIGKILLed in the middle of it.
                    raise
                except Exception:  # metrics bookkeeping must never stop the worker
                    log.exception("worker_metrics_compaction_failed", horse=horse)


def main() -> None:
    settings = get_settings()
    # A worker serves job metrics only with a multiprocess directory (compose sets one): the
    # job metrics are recorded in the work horses, and without it they are lost with each
    # horse, so there is nothing to serve. Serving, it follows the API's /metrics rule
    # (#165): no production worker without the token, refused before it takes any work.
    serving = bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR"))
    if serving:
        settings.require_metrics_token()
    configure_logging(json=settings.environment != "development")
    setup_tracing("quantlab-worker", synchronous=True)
    # A new name per start unless QUANTLAB_WORKER_NAME fixes one (#162): see worker_identity.
    name = worker_identity()
    # JSON only, trusted jobs only (#115): a worker without these unpickles the queue.
    worker = QuantlabWorker(
        [get_queue()],
        connection=redis_conn(),
        name=name,
        work_horse_killed_handler=horse_killed,  # out-of-memory kills (#163)
        **RQ_OPTIONS,
    )
    if serving:
        port = int(os.environ.get("QUANTLAB_WORKER_METRICS_PORT", "9101"))
        token = settings.metrics_token.get_secret_value() if settings.metrics_token else None
        serve_worker_metrics(port, token)
        log.info("worker_metrics", port=port)
    log.info("worker_start", name=name, queue=settings.queue_name)
    worker.work(with_scheduler=True, logging_level="WARNING")


if __name__ == "__main__":
    main()
