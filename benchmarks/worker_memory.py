"""Peak memory of backtest jobs in the real worker container (#163).

Against a running stack (``make app-up``, or a worktree's stack with its environment from
``scripts/stack-env.sh`` exported), this scales the worker service to one replica, and for
each configuration recreates it and submits one backtest through the API. While the job
runs it samples the container's anonymous memory every second (``memory.stat`` anon: what
runs out); afterwards it reads ``memory.peak`` (which also counts reclaimable page cache),
the ``oom_kill`` count and the job's outcome. **Synthetic data**; measured, never
extrapolated.

Usage::

    QUANTLAB_BENCH_API=http://localhost:<api port> \
      uv run python benchmarks/worker_memory.py --assets 400 --universe 100 200 400 \
      --rebalance daily monthly --strategy equal_weight
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness
from load_and_scaling import wait

API = os.environ.get("QUANTLAB_BENCH_API", "http://localhost:8000")
COMPOSE = ["docker", "compose", "--profile", "app"]


def _compose(*args: str) -> str:
    return subprocess.run([*COMPOSE, *args], check=True, capture_output=True, text=True).stdout


def _worker() -> str:
    """The one worker container (the sweep scales the service to a single replica)."""
    (cid,) = _compose("ps", "-q", "worker").split()
    return cid


def _cgroup(cid: str, name: str) -> str:
    return subprocess.run(
        ["docker", "exec", cid, "cat", f"/sys/fs/cgroup/{name}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _stat(cid: str, key: str) -> int:
    for line in _cgroup(cid, "memory.stat").splitlines():
        k, _, v = line.partition(" ")
        if k == key:
            return int(v)
    return -1


def _oom_kills(cid: str) -> int:
    for line in _cgroup(cid, "memory.events").splitlines():
        if line.startswith("oom_kill "):
            return int(line.split()[1])
    return -1


class _AnonSampler(threading.Thread):
    """The largest anonymous (non-file) memory of the container, sampled every second.

    ``memory.peak`` also counts page cache, which the kernel reclaims before it kills a
    process, so a job that completed can still show a peak at the limit. Anonymous memory
    is what runs out.
    """

    def __init__(self, cid: str) -> None:
        super().__init__(daemon=True)
        self.cid, self.peak, self.samples = cid, 0, 0
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.peak = max(self.peak, _stat(self.cid, "anon"))
                self.samples += 1
            except (subprocess.CalledProcessError, ValueError):
                pass  # the container restarting or gone: nothing to sample
            self._stop.wait(1.0)

    def stop(self) -> int:
        self._stop.set()
        self.join()
        return self.peak


def setup(c: httpx.Client, assets: int, start: str, end: str) -> dict[str, Any]:
    tok = c.post(
        "/api/v1/auth/register",
        json={
            "email": f"mem-{uuid.uuid4().hex[:8]}@example.com",
            "password": "Memory-test-2026",
            "display_name": "memory",
        },
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    ws = c.get("/api/v1/workspaces", headers=h).json()[0]["id"]
    job = c.post(
        f"/api/v1/workspaces/{ws}/datasets/synthetic",
        headers=h,
        json={
            "slug": "memory",
            "name": "memory",
            "n_assets": assets,
            "start": start,
            "end": end,
            "seed": 7,
        },
    ).json()
    (done,) = wait(c, h, [job["id"]])
    assert done["status"] == "completed", done
    dv = c.get(f"/api/v1/workspaces/{ws}/dataset-versions", headers=h).json()[0]
    return {"h": h, "ws": ws, "dv": dv["id"], "rows": dv["rows"], "symbols": dv["symbol_count"]}


def one(
    c: httpx.Client, s: dict[str, Any], universe: int, rebalance: str, strategy: str, start: str
) -> dict[str, Any]:
    # A new container is a new cgroup, so its peak belongs to this job alone. One replica,
    # so the job runs in the container measured.
    _compose("up", "-d", "--no-deps", "--force-recreate", "--scale", "worker=1", "--wait", "worker")
    cid = _worker()
    kills = _oom_kills(cid)
    sampler = _AnonSampler(cid)
    r = c.post(
        f"/api/v1/workspaces/{s['ws']}/experiments",
        headers=s["h"],
        json={
            "name": f"mem-{universe}-{rebalance}-{strategy}",
            "dataset_version_id": s["dv"],
            "strategy_key": strategy,
            "strategy_params": {},
            "config": {
                "start": start,
                "rebalance": rebalance,
                "universe": [f"SYN{i:03d}" for i in range(1, universe + 1)],
            },
        },
    )
    if r.status_code != 202:
        return {
            "universe": universe,
            "rebalance": rebalance,
            "strategy": strategy,
            "submitted": r.status_code,
            "error": r.json().get("error"),
        }
    t = time.time()
    sampler.start()
    (job,) = wait(c, s["h"], [r.json()["job"]["id"]])
    anon = sampler.stop()
    assert _worker() == cid, "the worker container changed during the job"
    return {
        "universe": universe,
        "rebalance": rebalance,
        "strategy": strategy,
        "status": job["status"],
        "attempts": job.get("attempts"),
        "error": job.get("error"),
        "container": cid[:12],
        "wall_s": round(time.time() - t, 1),
        "anon_peak_mb": round(anon / 2**20, 1),
        "anon_samples": sampler.samples,
        "memory_peak_mb": round(int(_cgroup(cid, "memory.peak")) / 2**20, 1),
        "memory_max_mb": round(int(_cgroup(cid, "memory.max")) / 2**20, 1),
        "oom_kills": _oom_kills(cid) - kills,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", type=int, default=400, help="assets in the dataset")
    ap.add_argument("--start", default="2005-01-03")
    ap.add_argument("--end", default="2023-12-29")
    ap.add_argument("--universe", type=int, nargs="+", default=[100, 200, 400])
    ap.add_argument("--rebalance", nargs="+", default=["daily", "monthly"])
    ap.add_argument("--strategy", nargs="+", default=["equal_weight"])
    ap.add_argument("--out", type=Path)
    # The commit the stack's images were built from, when this script runs from a copy.
    ap.add_argument("--stack-commit")
    a = ap.parse_args()
    _compose("up", "-d", "--no-deps", "--scale", "worker=1", "--wait", "worker")
    c = httpx.Client(base_url=API, timeout=60)
    s = setup(c, a.assets, a.start, a.end)
    print(json.dumps({k: s[k] for k in ("rows", "symbols")}), flush=True)
    load = harness.load_snapshot()
    rows = []
    for n, reb, strat in itertools.product(a.universe, a.rebalance, a.strategy):
        rows.append(one(c, s, n, reb, strat, a.start))
        print(json.dumps(rows[-1]), flush=True)
    res = harness.finalize(
        {
            "mode": "worker_memory",
            "stack_commit": a.stack_commit,
            "environment": harness.machine(),
            "data_is_synthetic": True,
            "dataset": {
                "assets": a.assets,
                "start": a.start,
                "end": a.end,
                "rows": s["rows"],
                "symbols": s["symbols"],
            },
            "rows": rows,
        },
        load,
    )
    if a.out:
        a.out.write_text(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
