"""Simulated work horses for test_worker_metrics.py's compaction tests (#165).

Run in a fresh interpreter: multiprocess mode applies only if PROMETHEUS_MULTIPROC_DIR is
set before prometheus_client creates the metrics, when quantlab_api is imported. Each job
is a forked child recording what a horse records; the parent then does what QuantlabWorker
does after a horse exits. A thread scrapes the endpoint all along, as Prometheus might.

    python -m tests.unit.metrics_compaction_child <jobs> <compact|slow|keep>

``slow`` compacts with a 20 ms pause before each dead file is removed, so a scrape that
did not wait for the compaction would see that horse's samples twice.

Prints JSON: the files left, the outcome total after each job and every total a concurrent
scrape saw, and the final samples.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import time

assert os.environ.get("PROMETHEUS_MULTIPROC_DIR"), "set by the test"

from prometheus_client import multiprocess  # noqa: E402

from quantlab_api import observability as O  # noqa: E402

APP = O.worker_metrics_app(None)


def scrape() -> str:
    environ = {"PATH_INFO": "/metrics", "REQUEST_METHOD": "GET", "wsgi.input": io.BytesIO()}
    return b"".join(APP(environ, lambda *_: None)).decode()


def value(text: str, name: str, **labels: str) -> float | None:
    for line in text.splitlines():
        m = re.fullmatch(r"([a-zA-Z_:][\w:]*)(?:\{(.*)\})? (\S+)", line)
        if m and m.group(1) == name:
            got = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2) or ""))
            if all(got.get(k) == v for k, v in labels.items()):
                return float(m.group(3))
    return None


def outcomes(text: str) -> float:
    return value(text, "quantlab_job_outcomes_total", kind="backtest", outcome="completed") or 0.0


def horse(i: int) -> None:
    O.JOBS_RUNNING.inc()
    O.JOB_RUN_SECONDS.labels("backtest", "completed").observe(2.0)
    O.JOB_OUTCOMES.labels("backtest", "completed").inc()
    O.DATASET_ROWS.set(1000 + i)
    O.JOBS_RUNNING.dec()


def main() -> None:
    jobs, mode = int(sys.argv[1]), sys.argv[2]
    compact = mode in ("compact", "slow")
    if mode == "slow":
        remove = os.remove

        def slow_remove(path: str) -> None:
            time.sleep(0.02)
            remove(path)

        O.os.remove = slow_remove  # type: ignore[attr-defined]
    seen: list[float] = []
    errors: list[str] = []
    stop = threading.Event()

    def scraper() -> None:
        # A failed scrape is recorded, not fatal: without the lock, the collector can list
        # a dead horse's file that a compaction then removes before reading it.
        while not stop.is_set():
            try:
                seen.append(outcomes(scrape()))
            except Exception as exc:  # reported to the test
                errors.append(repr(exc))

    thread = threading.Thread(target=scraper)
    thread.start()
    after_each = []
    for i in range(jobs):
        pid = os.fork()
        if pid == 0:
            horse(i)
            os._exit(0)
        os.waitpid(pid, 0)
        multiprocess.mark_process_dead(pid)
        if compact:
            O.compact_dead_process(pid)
        after_each.append(outcomes(scrape()))
    seen.append(outcomes(scrape()))  # one more after the last compaction, on its own
    stop.set()
    thread.join()
    text = scrape()
    print(
        json.dumps(
            {
                "files": sorted(os.listdir(os.environ["PROMETHEUS_MULTIPROC_DIR"])),
                "after_each": after_each,
                "seen": seen,
                "errors": errors[:5],
                "rows": value(text, "quantlab_dataset_rows"),
                "run_count": value(text, "quantlab_job_run_seconds_count", kind="backtest"),
                "run_sum": value(text, "quantlab_job_run_seconds_sum", kind="backtest"),
                "le_2": value(text, "quantlab_job_run_seconds_bucket", kind="backtest", le="2.0"),
                "running": value(text, "quantlab_jobs_running"),
            }
        )
    )


if __name__ == "__main__":
    main()
