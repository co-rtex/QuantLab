"""A worker's Prometheus multiprocess directory against the number of jobs run (#165).

Every job runs in a new work horse (a new pid), and multiprocess mode keeps a file per kind
per pid. This measures what that costs, as rq leaves it (``keep``) and as QuantlabWorker
does (``compact``). Each job is a forked child recording what a backtest's horse records:
queue wait, run time, outcome, the running gauge, DB statement latency and a cache event.
The parent then does the bookkeeping QuantlabWorker does after a horse exits. After each
checkpoint: the file count, apparent and allocated bytes (on a tmpfs, the allocated bytes
are memory charged to the container) and the scrape time, as the median and max of 20
``generate_latest`` calls through the worker endpoint's collector. With ``compact``, also
the time per fold.

Not a harness benchmark: it runs inside the worker image, where the tmpfs and the
prometheus-client version are the deployed ones, and the caller records the machine and
its load (results/*-worker-metric-files-*.log). Run it in an empty tmpfs owned by the
image's user, as compose mounts it:

    docker run --rm -i --tmpfs /tmp/prometheus:uid=10001,gid=10001,mode=0700 \\
      -e PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus --entrypoint python \\
      <project>-worker - compact 100 1000 5000 < benchmarks/worker_metric_files.py
"""

from __future__ import annotations

import os
import statistics
import sys
import time

d = os.environ["PROMETHEUS_MULTIPROC_DIR"]
assert not os.listdir(d), "start from an empty directory"

from prometheus_client import CollectorRegistry, generate_latest, multiprocess  # noqa: E402

from quantlab_api import observability as obs  # noqa: E402

# The worker process's own files (its unlabelled gauges open them at import).
print(f"after import: {len(os.listdir(d))} file(s)", flush=True)


def horse() -> None:
    obs.JOBS_RUNNING.inc()
    obs.JOB_QUEUE_WAIT_SECONDS.labels("backtest").observe(0.2)
    for verb in ("SELECT", "UPDATE", "INSERT"):
        for _ in range(5):
            obs.DB_QUERY_SECONDS.labels(verb).observe(0.002)
    obs.CACHE_EVENTS.labels("bars", "hit").inc()
    obs.JOB_RUN_SECONDS.labels("backtest", "completed").observe(3.0)
    obs.JOB_OUTCOMES.labels("backtest", "completed").inc()
    obs.JOBS_RUNNING.dec()


def measure(n: int) -> None:
    registry = CollectorRegistry()
    obs.worker_collector()(registry)
    times = []
    for _ in range(20):
        t = time.perf_counter()
        body = generate_latest(registry)
        times.append(time.perf_counter() - t)
    paths = [os.path.join(d, f) for f in os.listdir(d)]
    apparent = sum(os.path.getsize(p) for p in paths)
    allocated = sum(os.stat(p).st_blocks * 512 for p in paths)
    outcomes = next(
        float(line.split()[-1])
        for line in body.decode().splitlines()
        if line.startswith('quantlab_job_outcomes_total{kind="backtest",outcome="completed"}')
    )
    print(
        f"jobs={n:5d} files={len(paths):5d} apparent={apparent / 2**20:8.1f} MiB "
        f"allocated={allocated / 2**20:7.2f} MiB "
        f"scrape median={statistics.median(times) * 1000:7.1f} ms max={max(times) * 1000:7.1f} ms "
        f"body={len(body)} B outcomes_total={outcomes:.0f}",
        flush=True,
    )


def main() -> None:
    mode = sys.argv[1]
    assert mode in ("keep", "compact"), "keep: as rq leaves it; compact: as QuantlabWorker does"
    print(f"mode={mode}", flush=True)
    done = 0
    folds: list[float] = []
    for target in sorted(int(a) for a in sys.argv[2:]):
        while done < target:
            pid = os.fork()
            if pid == 0:
                horse()
                os._exit(0)
            os.waitpid(pid, 0)
            multiprocess.mark_process_dead(pid)
            if mode == "compact":
                t = time.perf_counter()
                obs.compact_dead_process(pid)
                folds.append(time.perf_counter() - t)
            done += 1
        measure(done)
        if folds:
            print(
                f"  fold per job: median={statistics.median(folds) * 1000:.2f} ms "
                f"max={max(folds) * 1000:.2f} ms",
                flush=True,
            )


main()
