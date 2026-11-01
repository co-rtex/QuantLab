"""A real forking worker through a Redis outage, for test_redis_outage.py (#185).

Two jobs are queued. The first job's handler, in the work horse, starts the outage (a flag
file both processes see) and runs for a few seconds, so the worker's horse monitor
heartbeats into a dead Redis several times. The outage lasts past the job's end, through
RQ's bookkeeping, until the test removes the flag. Then the worker dequeues the second
job. The queue is fakeredis whose connections fail while the flag exists; job state is in
the test PostgreSQL database.

    python -m tests.integration.redis_outage_child <flag> <quantlab|plain> <job 1> <job 2>
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import fakeredis
import redis
from fakeredis._clients._sync import FakeRedisConnection
from rq import Queue, Worker

from quantlab_api import jobs as J
from quantlab_api.worker import QuantlabWorker

FLAG = sys.argv[1]


class Flaky(FakeRedisConnection):
    def read_response(self, **kwargs: Any) -> Any:
        if os.path.exists(FLAG):
            raise redis.exceptions.ConnectionError("injected: Redis is down")
        return super().read_response(**kwargs)


@J.handler("outage.probe")
def probe(ctx: J.JobContext):  # type: ignore[no-untyped-def]
    if ctx.payload.get("outage"):
        open(FLAG, "w").close()  # Redis goes down mid-job
        for _ in range(8):  # the monitor heartbeats about every second meanwhile
            time.sleep(0.5)
            ctx.heartbeat(force=True)  # PostgreSQL: unaffected
    return lambda db: {"message": "done"}


def main() -> None:
    kind, first, second = sys.argv[2], sys.argv[3], sys.argv[4]
    conn = fakeredis.FakeRedis(connection_class=Flaky)
    J.set_redis(conn)
    queue = Queue("outage", connection=conn, **J.RQ_OPTIONS)  # type: ignore[arg-type]
    for job_id in (first, second):
        queue.enqueue(J.execute_job, job_id, job_timeout=120)
    cls = QuantlabWorker if kind == "quantlab" else Worker
    worker = cls(
        [queue],
        connection=conn,
        name=J.worker_identity(),
        job_monitoring_interval=1,
        **J.RQ_OPTIONS,  # type: ignore[arg-type]
    )
    worker.work(burst=True, logging_level="WARNING")


if __name__ == "__main__":
    main()
