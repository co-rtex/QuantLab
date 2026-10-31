"""Measurement discipline shared by the benchmarks (#24).

* **Variance:** every timed quantity is measured ``repeats`` times and reported as the
  median and interquartile range, with the raw samples kept.
* **Machine and load:** every result records the 1/5/15-minute load, CPU count, memory,
  Docker VM memory, the git commit, and ``contended: true`` when the 1-minute load
  exceeds half the cores at the start or the end of the run. Contended numbers are not
  published (docs/BENCHMARKS.md).
* **Cache state:** reads are *warm* (OS page cache as it is) unless ``--cold`` is given
  and the platform lets this process drop caches, in which case each repeat starts cold.
  Dropping caches needs privileges (macOS ``purge`` through ``sudo -n``; Linux writing
  ``/proc/sys/vm/drop_caches`` as root). Without them the run stays labelled warm and
  records why.
* **In-process vs HTTP:** rows measured inside this Python process (the engine, library
  reads) say ``in_process: true``; rows measured over HTTP against a running API say
  ``false``.
* **Pools:** :func:`warmed_pool` starts every worker and imports the engine in each before
  any timing, and checks that each worker took part.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------- variance
def stats(samples_s: list[float], *, in_process: bool, cache: str) -> dict[str, Any]:
    """Median and IQR (ms) of seconds-valued samples, with the raw samples (ms)."""
    if not samples_s:
        return {"n": 0, "median_ms": None, "reason": "no samples", "in_process": in_process}
    ms = np.asarray(samples_s, dtype=float) * 1000
    q1, med, q3 = (float(x) for x in np.percentile(ms, [25, 50, 75]))
    return {
        "n": int(ms.size),
        "median_ms": round(med, 3),
        "iqr_ms": round(q3 - q1, 3),
        "p25_ms": round(q1, 3),
        "p75_ms": round(q3, 3),
        "min_ms": round(float(ms.min()), 3),
        "max_ms": round(float(ms.max()), 3),
        "samples_ms": [round(float(x), 3) for x in ms],
        "in_process": in_process,
        "cache": cache,
    }


def repeat(
    fn: Callable[[], Any], repeats: int, before: Callable[[], None] | None = None
) -> list[float]:
    """Time ``fn`` ``repeats`` times (seconds), calling ``before`` untimed each time."""
    out = []
    for _ in range(repeats):
        if before is not None:
            before()
        t = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t)
    return out


def repeat_each(fn: Callable[[Any], Any], items: Any) -> list[float]:
    """Time ``fn(item)`` once per item (seconds)."""
    out = []
    for item in items:
        t = time.perf_counter()
        fn(item)
        out.append(time.perf_counter() - t)
    return out


# --------------------------------------------------------------- machine metadata
def _run(cmd: list[str], timeout: float = 5) -> str | None:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def is_contended(load_1m: float, cpus: int) -> bool:
    """The publication rule: a 1-minute load above half the cores is contended."""
    return load_1m > cpus / 2


def load_snapshot() -> dict[str, Any]:
    cpus = os.cpu_count() or 1
    l1, l5, l15 = os.getloadavg()
    return {
        "load_1m": round(l1, 2),
        "load_5m": round(l5, 2),
        "load_15m": round(l15, 2),
        "uptime": _run(["uptime"]),
        "contended": is_contended(l1, cpus),
    }


def machine() -> dict[str, Any]:
    """Hardware, software and repository state for a result file."""
    mem_total = _run(["docker", "info", "--format", "{{.MemTotal}}"])
    sha = _run(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
    dirty = _run(["git", "-C", str(ROOT), "status", "--porcelain"])
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "memory_gb": round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1),
        "docker_vm_memory_gb": round(int(mem_total) / 2**30, 2)
        if mem_total and mem_total.isdigit()
        else None,
        "python": platform.python_version(),
        "git_sha": sha,
        "git_dirty": bool(dirty) if dirty is not None else None,
    }


def finalize(result: dict[str, Any], start_load: dict[str, Any]) -> dict[str, Any]:
    """Attach load at both ends; contended if either end was."""
    end_load = load_snapshot()
    result["load"] = {"start": start_load, "end": end_load}
    result["contended"] = bool(start_load["contended"] or end_load["contended"])
    return result


# ------------------------------------------------------------------- cache state
def cold_capability() -> tuple[Callable[[], None] | None, str]:
    """A function that drops the OS page cache, or None with the reason it can't."""
    if sys.platform == "darwin":
        if shutil.which("purge") is None:
            return None, "purge not found"
        probe = subprocess.run(["sudo", "-n", "purge"], capture_output=True, check=False)
        if probe.returncode != 0:
            return None, "macOS purge needs sudo; run `sudo -v` first (opt-in)"

        def purge() -> None:
            subprocess.run(["sudo", "-n", "purge"], check=True)

        return purge, "macOS purge"
    if sys.platform.startswith("linux"):
        target = Path("/proc/sys/vm/drop_caches")
        if os.geteuid() != 0 or not os.access(target, os.W_OK):
            return None, "needs root to write /proc/sys/vm/drop_caches"

        def drop() -> None:
            os.sync()
            target.write_text("3\n")

        return drop, "linux drop_caches"
    return None, f"unsupported platform {sys.platform}"


# ------------------------------------------------------------------------- pools
_BARRIER: Any = None


def _init(barrier: Any) -> None:
    global _BARRIER
    _BARRIER = barrier
    import quantlab.backtest.engine
    import quantlab.strategies.library  # noqa: F401


def _arrive(_: int) -> int:
    _BARRIER.wait(timeout=120)  # every worker must arrive: no worker takes two
    return os.getpid()


@contextmanager
def warmed_pool(workers: int) -> Iterator[tuple[ProcessPoolExecutor, int]]:
    """A process pool whose every worker has started and imported the engine.

    Yields the pool and the number of distinct workers that warmed (always ``workers``,
    or the barrier would have timed out).
    """
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(workers)
    with ProcessPoolExecutor(
        max_workers=workers, mp_context=ctx, initializer=_init, initargs=(barrier,)
    ) as ex:
        pids = set(ex.map(_arrive, range(workers)))
        yield ex, len(pids)
