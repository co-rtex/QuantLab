"""Compaction changes nothing a scrape sees (#165), for every kind it archives.

Run by test_worker_metrics.py in a fresh interpreter (multiprocess mode must be on before
the metrics exist). Forked "horses" record into a counter, a histogram and gauges in each
archived mode (sum, max, min, mostrecent), with labels full of quotes, backslashes and
non-ASCII. The directory is then copied: one copy is scraped as rq leaves it, the other
after compact_dead_process on every dead pid. Prints both scrapes as JSON.

    python -m tests.unit.metrics_equivalence_child <horses>
"""

from __future__ import annotations

import json
import os
import random
import shutil
import sys
import time

assert os.environ.get("PROMETHEUS_MULTIPROC_DIR"), "set by the test"

from prometheus_client import (  # noqa: E402
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

from quantlab_api.observability import compact_dead_process, worker_collector  # noqa: E402

LABELS = ["plain", 'quo"te', "back\\slash", "naïve ünïcode", "new\nline", "a,b=c"]
C = Counter("eq_events_total", "events", ["k"])
H = Histogram("eq_seconds", "seconds", ["k"], buckets=(0.1, 1, 10))
G = {
    mode: Gauge(f"eq_gauge_{mode}", f"gauge {mode}", ["k"], multiprocess_mode=mode)
    for mode in ("sum", "max", "min", "mostrecent")
}


def horse(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(rng.randint(1, 4)):
        k = rng.choice(LABELS)
        C.labels(k).inc(rng.randint(1, 5))
        H.labels(k).observe(rng.choice([0.05, 0.5, 5.0, 50.0]))
        for g in G.values():
            g.labels(k).set(rng.uniform(-100, 100))
        time.sleep(0.001)  # distinct mostrecent timestamps


def scrape(path: str) -> list[str]:
    """As the worker's endpoint scrapes: its collector fixes the order files are added in,
    so float sums come out bit-identical whether or not the dead horses are folded."""
    registry = CollectorRegistry()
    worker_collector()(registry, path=path)
    return sorted(generate_latest(registry).decode().splitlines())


def main() -> None:
    horses = int(sys.argv[1])
    d = os.environ["PROMETHEUS_MULTIPROC_DIR"]
    pids = []
    for i in range(horses):
        pid = os.fork()
        if pid == 0:
            horse(i)
            os._exit(0)
        os.waitpid(pid, 0)
        multiprocess.mark_process_dead(pid)
        pids.append(pid)
    compacted = d.rstrip("/") + "-compacted"
    shutil.copytree(d, compacted)
    for pid in pids:
        compact_dead_process(pid, compacted)
    print(
        json.dumps(
            {
                "plain": scrape(d),
                "compacted": scrape(compacted),
                "files_plain": len(os.listdir(d)),
                "files_compacted": sorted(os.listdir(compacted)),
            }
        )
    )


if __name__ == "__main__":
    main()
