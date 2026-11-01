"""RQ worker entrypoint: ``quantlab-worker``.

Workers are stateless; job state lives in PostgreSQL (see ``jobs.py``). Run several
replicas for parallelism; each executes one job at a time in a forked work horse so a
crashing or runaway job cannot take the worker down.
"""

from __future__ import annotations

import os
from typing import Any

import redis
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

# Redis unreachable or not answering. A job's state is in PostgreSQL, whose lease and
# keepalive run without Redis (jobs.py), so these are not a reason to stop a worker.
REDIS_DOWN = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


class QuantlabWorker(Worker):
    """RQ's forking worker. Once a work horse has exited, however it ended (#165):

    * its live gauges (``quantlab_jobs_running``) are dropped: a horse killed mid-job never
      decrements them, and they would read 1 until the container restarts;
    * its other metric files are folded into the archive (``compact_dead_process``), so the
      directory and each scrape stay the same size however many jobs have run.

    It also outlasts a Redis outage (#185). RQ's horse monitor heartbeats through Redis,
    and an error there quit the worker: in a container, pid 1 exited, the running horse died
    with it, the job lost an attempt and waited out its lease, and the container restarted
    in a loop until Redis came back. Here every write of RQ's own bookkeeping that cannot
    reach Redis is logged and skipped: heartbeats (so the monitor keeps waiting on the
    horse), maintenance, the suspension check and the worker's state at the top of each
    pass, the record of the job after the horse exits (its outcome is already in
    PostgreSQL), and a stopping worker's death. The next dequeue then waits for Redis, as
    an idle worker's always did.
    """

    _last_horse: int = 0

    def _redis_down(self, during: str, exc: Exception) -> None:
        log.warning("worker_redis_unavailable", during=during, error=str(exc))

    def heartbeat(self, timeout: int | None = None, pipeline: Any = None) -> None:
        try:
            super().heartbeat(timeout, pipeline=pipeline)
        except REDIS_DOWN as exc:
            self._redis_down("heartbeat", exc)

    def maintain_heartbeats(self, job: Any, execution: Any) -> None:
        # Called by the horse monitor while a job runs: an error here quit the worker.
        try:
            super().maintain_heartbeats(job, execution)
        except REDIS_DOWN as exc:
            self._redis_down("job_heartbeat", exc)

    def run_maintenance_tasks(self) -> None:
        try:
            super().run_maintenance_tasks()  # type: ignore[no-untyped-call]
        except REDIS_DOWN as exc:
            self._redis_down("maintenance", exc)

    def check_for_suspension(self, burst: bool) -> None:
        # At the top of every pass of the work loop. Unreachable, Redis is taken as not
        # suspended: the dequeue that follows waits for it anyway.
        try:
            super().check_for_suspension(burst)
        except REDIS_DOWN as exc:
            self._redis_down("suspension_check", exc)

    def set_state(self, state: str, pipeline: Any = None) -> None:
        # The dequeue sets the worker idle before its own reconnecting loop. With a
        # pipeline nothing is sent here, so only a direct write can fail.
        try:
            super().set_state(state, pipeline=pipeline)
        except REDIS_DOWN as exc:
            self._redis_down("worker_state", exc)

    def register_death(self) -> None:
        # A worker stopped during an outage: its registration expires in Redis by itself.
        try:
            super().register_death()  # type: ignore[no-untyped-call]
        except REDIS_DOWN as exc:
            self._redis_down("register_death", exc)

    def fork_work_horse(self, job: Any, queue: Any) -> None:
        super().fork_work_horse(job, queue)  # the horse itself never returns (os._exit)
        # RQ resets horse_pid to 0 once it has reaped the horse, so keep it here.
        self._last_horse = self.horse_pid

    def execute_job(self, job: Any, queue: Any) -> None:
        try:
            try:
                super().execute_job(job, queue)
            except REDIS_DOWN as exc:
                # Before the fork, or after the horse exited: the monitor waits on the horse
                # through an outage (above). RQ's record of the job is incomplete then; its
                # outcome is in PostgreSQL, and a job never started is redelivered by the
                # reaper.
                self._redis_down("job_bookkeeping", exc)
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
