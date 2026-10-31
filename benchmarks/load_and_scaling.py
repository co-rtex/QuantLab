"""Concurrent HTTP load and RQ worker scaling against the containerized stack.

Usage (stack from ``make app-up``)::

    uv run python benchmarks/load_and_scaling.py load
    uv run python benchmarks/load_and_scaling.py scaling --workers 1 2 4 --jobs 24

``scaling`` resizes the compose ``worker`` service with ``docker compose up --scale``.
Results are written to ``benchmarks/results/<timestamp>-<mode>[-contended].json`` with the
machine and load metadata of ``benchmarks/harness.py``. Every row here is measured over
HTTP against the stack (``in_process: false``); ``--repeats`` repeats each load level.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness

# Default: the main checkout's stack. For a worktree's stack, set QUANTLAB_BENCH_API to
# its API port (QUANTLAB_API_PORT from scripts/stack-env.sh).
API = os.environ.get("QUANTLAB_BENCH_API", "http://localhost:8000")
OUT = Path(__file__).resolve().parent / "results"


def setup(client: httpx.Client, assets: int = 200) -> dict[str, str]:
    tok = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"load-{uuid.uuid4().hex[:8]}@example.com",
            "password": "Load-test-2026",
            "display_name": "load",
        },
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    ws = client.get("/api/v1/workspaces", headers=h).json()[0]["id"]
    job = client.post(
        f"/api/v1/workspaces/{ws}/datasets/synthetic",
        headers=h,
        json={
            "slug": "load",
            "name": "load",
            "n_assets": assets,
            "start": "2017-01-02",
            "end": "2023-12-29",
            "seed": 3,
        },
    ).json()
    wait(client, h, [job["id"]])
    dv = client.get(f"/api/v1/workspaces/{ws}/dataset-versions", headers=h).json()[0]["id"]
    return {"token": tok, "ws": ws, "dv": dv}


def wait(
    client: httpx.Client, h: dict[str, str], ids: list[str], timeout: float = 1800
) -> list[dict[str, Any]]:
    t0 = time.time()
    while time.time() - t0 < timeout:
        jobs = [client.get(f"/api/v1/jobs/{i}", headers=h).json() for i in ids]
        if all(j["status"] not in ("queued", "running") for j in jobs):
            return jobs
        time.sleep(0.5)
    raise TimeoutError


async def hammer(
    path: str, headers: dict[str, str], concurrency: int, total: int
) -> dict[str, Any]:
    lat: list[float] = []
    errors = 0
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=API, timeout=30, limits=limits) as c:

        async def one() -> None:
            nonlocal errors
            async with sem:
                t = time.perf_counter()
                r = await c.get(path, headers=headers)
                lat.append(time.perf_counter() - t)
                if r.status_code != 200:
                    errors += 1

        t0 = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(total)))
        wall = time.perf_counter() - t0
    ms = np.array(lat) * 1000
    return {
        "concurrency": concurrency,
        "requests": total,
        "errors": errors,
        "throughput_rps": round(total / wall, 1),
        "p50_ms": round(float(np.percentile(ms, 50)), 2),
        "p95_ms": round(float(np.percentile(ms, 95)), 2),
        "p99_ms": round(float(np.percentile(ms, 99)), 2),
        "in_process": False,
    }


def _spread(runs: list[dict[str, Any]], key: str) -> dict[str, Any]:
    xs = np.asarray([r[key] for r in runs], dtype=float)
    q1, med, q3 = np.percentile(xs, [25, 50, 75])
    return {"median": round(float(med), 2), "iqr": round(float(q3 - q1), 2), "samples": xs.tolist()}


def load(repeats: int = 3) -> dict[str, Any]:
    c = httpx.Client(base_url=API, timeout=60)
    s = setup(c, assets=50)
    h = {"Authorization": f"Bearer {s['token']}"}
    c.get(f"/api/v1/dataset-versions/{s['dv']}/bars/SYN001?adjusted=true", headers=h)  # warm
    endpoints = {
        "experiments_list": f"/api/v1/workspaces/{s['ws']}/experiments",
        "bars_cached_adjusted": f"/api/v1/dataset-versions/{s['dv']}/bars/SYN001?adjusted=true",
        "health": "/api/v1/health",
    }
    out: dict[str, Any] = {}
    for name, path in endpoints.items():
        levels = []
        for cc in (1, 8, 32, 64):
            runs = [asyncio.run(hammer(path, h, cc, max(200, cc * 20))) for _ in range(repeats)]
            levels.append(
                {
                    "concurrency": cc,
                    "throughput_rps": _spread(runs, "throughput_rps"),
                    "p50_ms": _spread(runs, "p50_ms"),
                    "p99_ms": _spread(runs, "p99_ms"),
                    "errors": sum(r["errors"] for r in runs),
                    "runs": runs,
                    "in_process": False,
                }
            )
        out[name] = levels
    return out


def scaling(workers: list[int], jobs: int) -> dict[str, Any]:
    c = httpx.Client(base_url=API, timeout=60)
    s = setup(c)
    h = {"Authorization": f"Bearer {s['token']}"}
    results = []
    for n in workers:
        subprocess.run(
            [
                "docker",
                "compose",
                "--profile",
                "app",
                "up",
                "-d",
                "--no-deps",
                "--scale",
                f"worker={n}",
                "--wait",
                "worker",
            ],
            check=True,
            capture_output=True,
        )
        time.sleep(3)
        t0 = time.time()
        ids = []
        for i in range(jobs):
            r = c.post(
                f"/api/v1/workspaces/{s['ws']}/experiments",
                headers=h,
                json={
                    "name": f"scale-{n}-{i}",
                    "dataset_version_id": s["dv"],
                    "strategy_key": "momentum",
                    "strategy_params": {"lookback": 252, "skip": 21, "top_n": 10 + i % 10},
                    "config": {"start": "2018-01-02", "benchmark": "SYNIDX"},
                },
            )
            ids.append(r.json()["job"]["id"])
        done = wait(c, h, ids)
        wall = time.time() - t0
        ok = sum(j["status"] == "completed" for j in done)
        results.append(
            {
                "workers": n,
                "jobs": jobs,
                "completed": ok,
                "wall_s": round(wall, 2),
                "jobs_per_min": round(60 * ok / wall, 1),
            }
        )
        print(results[-1], flush=True)
    base = results[0]["wall_s"]
    for row in results:
        row["speedup"] = round(base / row["wall_s"], 2)
    return {"assets": 200, "sessions_traded": "2018-2023", "results": results}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["load", "scaling"])
    ap.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4])
    ap.add_argument("--jobs", type=int, default=24)
    ap.add_argument("--repeats", type=int, default=3)
    a = ap.parse_args()
    start = harness.load_snapshot()
    body = load(a.repeats) if a.mode == "load" else scaling(a.workers, a.jobs)
    res = harness.finalize(
        {"mode": a.mode, "environment": harness.machine(), "data_is_synthetic": True, **body},
        start,
    )
    OUT.mkdir(exist_ok=True)
    tag = "-contended" if res["contended"] else ""
    path = OUT / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{a.mode}{tag}.json"
    path.write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
