"""Peak memory of backtest jobs in the real worker container (#163).

Against a running stack (``make app-up``, or a worktree's stack with its environment from
``scripts/stack-env.sh`` exported), this scales the worker service to one replica, and for
each configuration restarts it and submits one backtest through the API. It then reads
the container's cgroup ``memory.peak`` (the job's peak: the container is new), the
``oom_kill`` count and the job's outcome. **Synthetic data**; measured, never
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


def _worker_cgroup(name: str) -> str:
    cid = _compose("ps", "-q", "worker").split()[0]
    return subprocess.run(
        ["docker", "exec", cid, "cat", f"/sys/fs/cgroup/{name}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _oom_kills() -> int:
    for line in _worker_cgroup("memory.events").splitlines():
        if line.startswith("oom_kill "):
            return int(line.split()[1])
    return -1


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
    # A new container is a new cgroup, so its peak belongs to this job alone.
    _compose("up", "-d", "--no-deps", "--force-recreate", "--wait", "worker")
    kills = _oom_kills()
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
    (job,) = wait(c, s["h"], [r.json()["job"]["id"]])
    return {
        "universe": universe,
        "rebalance": rebalance,
        "strategy": strategy,
        "status": job["status"],
        "attempts": job.get("attempts"),
        "error": job.get("error"),
        "wall_s": round(time.time() - t, 1),
        "memory_peak_mb": round(int(_worker_cgroup("memory.peak")) / 2**20, 1),
        "memory_max_mb": round(int(_worker_cgroup("memory.max")) / 2**20, 1),
        "oom_kills": _oom_kills() - kills,
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
