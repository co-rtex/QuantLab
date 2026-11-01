"""Reproducibility across processes, hash seeds and thread counts (#182). Synthetic data.

``tests/unit/repro_child.py`` runs the library chain behind an experiment (backtest,
analysis, validation suite, ML run, integrity report, Markdown and HTML report) and prints
a digest of each output. Here it runs in fresh interpreters that differ in
``PYTHONHASHSEED`` and in the BLAS/OpenMP thread count, and every digest must match. The
report's ``generated_at`` is the one documented timestamp and is pinned by the child.

The second test shows the comparison catches nondeterminism: with the ML models' random
state removed, two runs disagree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
THREAD_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)
VARIANTS = {
    "hashseed 0, 1 thread": ("0", "1"),
    "hashseed 1, 4 threads": ("1", "4"),
    "hashseed 4242, 2 threads": ("4242", "2"),
    "random hashseed, default threads": ("random", None),
}


def _spawn(hashseed: str, threads: str | None, *args: str) -> subprocess.Popen[str]:
    env = {k: v for k, v in os.environ.items() if k not in THREAD_VARS}
    env |= {"PYTHONHASHSEED": hashseed, "PYTHONPATH": str(ROOT)}
    if threads is not None:
        env |= dict.fromkeys(THREAD_VARS, threads)
    return subprocess.Popen(
        [sys.executable, "-m", "tests.unit.repro_child", *args],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _collect(procs: dict[str, subprocess.Popen[str]]) -> dict[str, dict[str, str]]:
    out = {}
    for name, p in procs.items():
        stdout, stderr = p.communicate(timeout=300)
        assert p.returncode == 0, f"{name}: {stderr[-2000:]}"
        out[name] = json.loads(stdout.strip().splitlines()[-1])
    return out


def test_every_output_is_identical_across_processes_hash_seeds_and_threads():
    runs = _collect({name: _spawn(seed, threads) for name, (seed, threads) in VARIANTS.items()})
    first_name, first = next(iter(runs.items()))
    assert set(first) >= {
        "fingerprint",
        "equity",
        "trades",
        "validation",
        "ml",
        "ml_predictions",
        "integrity",
        "report_markdown",
        "report_html",
    }
    for name, digests in runs.items():
        differ = sorted(k for k in first if digests[k] != first[k])
        assert not differ, f"{name} differs from {first_name} in {differ}"


def test_the_comparison_catches_an_unseeded_model():
    runs = _collect({f"unseeded {i}": _spawn("0", "1", "unseeded") for i in range(2)})
    a, b = runs.values()
    assert a["ml"] != b["ml"] or a["ml_predictions"] != b["ml_predictions"]
    assert a["fingerprint"] == b["fingerprint"]  # the backtest itself is untouched
