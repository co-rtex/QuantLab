"""RQ worker entrypoint: ``quantlab-worker``.

Workers are stateless; job state lives in PostgreSQL (see ``jobs.py``). Run several
replicas for parallelism; each executes one job at a time in a forked work horse so a
crashing or runaway job cannot take the worker down.
"""

from __future__ import annotations

import os

from rq import Worker

from quantlab_api import tasks  # noqa: F401 - registers job handlers
from quantlab_api.config import get_settings
from quantlab_api.jobs import get_queue, redis_conn, worker_identity
from quantlab_api.observability import configure_logging, log


def main() -> None:
    configure_logging(json=get_settings().environment != "development")
    name = os.environ.get("QUANTLAB_WORKER_NAME") or worker_identity().replace(":", "-")
    worker = Worker([get_queue()], connection=redis_conn(), name=name)
    log.info("worker_start", name=name, queue=get_settings().queue_name)
    worker.work(with_scheduler=True, logging_level="WARNING")


if __name__ == "__main__":
    main()
