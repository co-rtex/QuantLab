"""Peak memory of optimization jobs in the real worker container (#224).

As ``worker_memory.py`` does for backtests: against a running stack, the worker service is
scaled to one replica and recreated before each job, so the container's ``memory.peak``
belongs to that job alone, and its anonymous memory is sampled while the job runs. Jobs
take seconds, so this samples every 0.1 s, not every second. Each job is the worst case
of its size: every method, the longest look-back (1260), a benchmark, and the data's
whole history loaded (no as_of). **Synthetic data**; measured, never extrapolated.

Usage::

    QUANTLAB_BENCH_API=http://localhost:<api port> \
      uv run python benchmarks/optimize_memory.py --span 2020-01-02:2022-12-30 \
      --span 1995-01-03:2022-12-30 --symbols 10 50 100 --out benchmarks/results/<name>.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from quantlab.data.calendar import trading_days

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness
from load_and_scaling import wait
from worker_memory import API, _cgroup, _compose, _oom_kills, _stat, _worker, setup

JOB_TIMEOUT_S = 300
SAMPLE_S = 0.1


class _AnonSampler(threading.Thread):
    """The largest anonymous memory of the container, sampled every ``SAMPLE_S``."""

    def __init__(self, cid: str) -> None:
        super().__init__(daemon=True)
        self.cid, self.peak, self.samples = cid, 0, 0
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                self.peak = max(self.peak, _stat(self.cid, "anon"))
                self.samples += 1
            except (subprocess.CalledProcessError, ValueError):
                pass  # the container restarting or gone: nothing to sample
            self._halt.wait(SAMPLE_S)

    def stop(self) -> int:
        self._halt.set()
        self.join()
        return self.peak


def one(c: httpx.Client, s: dict[str, Any], symbols: int, sessions: int) -> dict[str, Any]:
    _compose("up", "-d", "--no-deps", "--force-recreate", "--scale", "worker=1", "--wait", "worker")
    cid = _worker()
    kills = _oom_kills(cid)
    idle = _stat(cid, "anon")
    sampler = _AnonSampler(cid)
    sampler.start()
    t = time.time()
    r = c.post(
        f"/api/v1/dataset-versions/{s['dv']}/optimizations",
        headers=s["h"],
        json={
            "symbols": [f"SYN{i:03d}" for i in range(1, symbols + 1)],
            "lookback": 1260,
            "benchmark": "SYNIDX",
        },
    )
    if r.status_code != 202:
        sampler.stop()
        return {"symbols": symbols, "submitted": r.status_code, "error": r.json().get("error")}
    run = r.json()
    try:
        (job,) = wait(c, s["h"], [run["job"]["id"]], timeout=JOB_TIMEOUT_S)
    except TimeoutError:
        job = c.get(f"/api/v1/jobs/{run['job']['id']}", headers=s["h"]).json()
        job["status"] = f"still {job['status']} after {JOB_TIMEOUT_S} s"
    anon = sampler.stop()
    assert _worker() == cid, "the worker container changed during the job"
    return {
        "symbols": symbols,
        # The bars loaded: the symbols and the benchmark over the whole history.
        "symbol_sessions": (symbols + 1) * sessions,
        "status": job["status"],
        "attempts": job.get("attempts"),
        "error": job.get("error"),
        "container": cid[:12],
        "wall_s": round(time.time() - t, 1),
        "idle_anon_mb": round(idle / 2**20, 1),
        "anon_peak_mb": round(anon / 2**20, 1),
        "anon_samples": sampler.samples,
        "memory_peak_mb": round(int(_cgroup(cid, "memory.peak")) / 2**20, 1),
        "memory_max_mb": round(int(_cgroup(cid, "memory.max")) / 2**20, 1),
        "oom_kills": _oom_kills(cid) - kills,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", type=int, default=120, help="assets in each dataset")
    ap.add_argument("--span", nargs="+", action="extend", required=True, help="START:END")
    ap.add_argument("--symbols", type=int, nargs="+", default=[10, 50, 100])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--stack-commit")
    a = ap.parse_args()
    _compose("up", "-d", "--no-deps", "--scale", "worker=1", "--wait", "worker")
    c = httpx.Client(base_url=API, timeout=60)
    load = harness.load_snapshot()
    datasets, rows = [], []
    for span in a.span:
        start, end = span.split(":")
        s = setup(c, a.assets, start, end)
        info = c.get(f"/api/v1/dataset-versions/{s['dv']}", headers=s["h"]).json()
        # As the admission estimate counts them: exchange sessions over the data's span.
        sessions = len(trading_days(info["start_date"], info["end_date"]))
        datasets.append(
            {
                "start": start,
                "end": end,
                "rows": s["rows"],
                "symbols": s["symbols"],
                "sessions": sessions,
                "is_synthetic": info.get("is_synthetic"),
            }
        )
        print(json.dumps(datasets[-1]), flush=True)
        for n in a.symbols:
            rows.append({"span": span, "sessions": sessions, **one(c, s, n, sessions)})
            print(json.dumps(rows[-1]), flush=True)
    res = harness.finalize(
        {
            "mode": "optimize_memory",
            "stack_commit": a.stack_commit,
            "environment": harness.machine(),
            "data_is_synthetic": True,
            "datasets": datasets,
            "rows": rows,
        },
        load,
    )
    if a.out:
        a.out.write_text(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
