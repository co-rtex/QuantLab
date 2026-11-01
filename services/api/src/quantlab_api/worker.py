"""RQ worker entrypoint: ``quantlab-worker``.

Workers are stateless; job state lives in PostgreSQL (see ``jobs.py``). Run several
replicas for parallelism; each executes one job at a time in a forked work horse so a
crashing or runaway job cannot take the worker down.
"""

from __future__ import annotations

from rq import Worker

from quantlab_api import tasks  # noqa: F401 - registers job handlers
from quantlab_api.config import get_settings
from quantlab_api.jobs import RQ_OPTIONS, get_queue, redis_conn, worker_identity
from quantlab_api.observability import configure_logging, log
from quantlab_api.tracing import setup_tracing


def main() -> None:
    configure_logging(json=get_settings().environment != "development")
    setup_tracing("quantlab-worker", synchronous=True)
    # A new name per start unless QUANTLAB_WORKER_NAME fixes one (#162): see worker_identity.
    name = worker_identity()
    # JSON only, trusted jobs only (#115): a worker without these unpickles the queue.
    worker = Worker([get_queue()], connection=redis_conn(), name=name, **RQ_OPTIONS)
    log.info("worker_start", name=name, queue=get_settings().queue_name)
    worker.work(with_scheduler=True, logging_level="WARNING")


if __name__ == "__main__":
    main()
