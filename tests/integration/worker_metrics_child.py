"""A real quantlab worker process for test_worker_metrics.py (#165).

Run in a fresh interpreter: multiprocess mode only applies if PROMETHEUS_MULTIPROC_DIR is
set before prometheus_client creates the metrics, which is when quantlab_api is imported,
and the pytest process imported it long before. It runs one job through a forking worker
(the job runs in a work horse, as in a container), serves this process's /metrics as
``quantlab-worker`` does, scrapes it with the token, and prints the text.

    python -m tests.integration.worker_metrics_child <port> <token> <worker> [<job id>]

``worker`` is ``quantlab`` (QuantlabWorker), ``plain`` (rq's Worker, the control) or
``none`` (no job: a restarted container's first scrape). A job's payload may ask its horse
to die mid-job: ``{"die": "horse"}`` (an OOM kill of the horse) or ``{"die": "worker"}``
(the whole container killed: the worker first, then the horse).

The queue is a process-local fakeredis, as in the other integration tests (CI has no
Redis); the job's state lives in the test PostgreSQL database.
"""

from __future__ import annotations

import os
import signal
import sys
import urllib.request

assert os.environ.get("PROMETHEUS_MULTIPROC_DIR"), "set by the test"

import fakeredis  # noqa: E402
from rq import Queue, Worker  # noqa: E402

from quantlab_api import jobs as J  # noqa: E402
from quantlab_api.observability import serve_worker_metrics  # noqa: E402
from quantlab_api.worker import QuantlabWorker  # noqa: E402


@J.handler("metrics.probe")
def probe(ctx: J.JobContext):  # type: ignore[no-untyped-def]
    ctx.heartbeat(progress=0.5, force=True)  # quantlab_jobs_running is 1 from here
    die = ctx.payload.get("die")
    if die == "worker":
        os.kill(os.getppid(), signal.SIGKILL)
    if die:
        os.kill(os.getpid(), signal.SIGKILL)
    return lambda db: {"message": "probe ok"}


def scrape(port: int, token: str) -> str:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/metrics", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode()


def main() -> None:
    port, token, kind = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    server = serve_worker_metrics(port, token)
    if kind != "none":
        conn = fakeredis.FakeRedis()
        J.set_redis(conn)
        queue = Queue("metrics-probe", connection=conn, **J.RQ_OPTIONS)  # type: ignore[arg-type]
        queue.enqueue(J.execute_job, sys.argv[4], job_timeout=120)
        cls = QuantlabWorker if kind == "quantlab" else Worker
        worker = cls([queue], connection=conn, name=J.worker_identity(), **J.RQ_OPTIONS)  # type: ignore[arg-type]
        worker.work(burst=True, logging_level="WARNING")
    sys.stdout.write(scrape(port, token))
    server.shutdown()


if __name__ == "__main__":
    main()
