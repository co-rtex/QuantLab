"""scripts/stack-env.sh warns when the Docker VM is below the default stack's minimum (#175).

Its stdout is eval'd by the Makefile and by shells, so the warning goes to stderr only, the
output is the same whatever the VM, and an unavailable or odd `docker` never fails it.
`docker` here is a stub on PATH.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIB = 1024 * 1024


def run(tmp_path: Path, docker: str | None, **env: str) -> subprocess.CompletedProcess[str]:
    """stack-env.sh for slot 21 with `docker` replaced by a script (None: no docker)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "docker"
    if docker is None:
        stub.unlink(missing_ok=True)
    else:
        stub.write_text(f"#!/bin/sh\n{docker}\n")
        stub.chmod(0o755)
    # Only the stub's directory and the system's: no real docker reachable.
    path = f"{bin_dir}:/usr/bin:/bin"
    full = {"PATH": path, "HOME": os.environ.get("HOME", "/tmp"), "QUANTLAB_SLOT": "21", **env}
    return subprocess.run(
        ["sh", "scripts/stack-env.sh"], cwd=ROOT, env=full, capture_output=True, text=True
    )


def reference(tmp_path: Path) -> str:
    out = run(tmp_path, None, QUANTLAB_SKIP_VM_CHECK="1")
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_a_vm_below_the_minimum_is_warned_about_on_stderr_only(tmp_path: Path) -> None:
    out = run(tmp_path, f"echo {3918 * MIB}")  # this machine's VM
    assert out.returncode == 0
    assert "warning: the Docker VM has 3918 MiB" in out.stderr
    assert "5120 MiB (5 GiB)" in out.stderr
    assert out.stdout == reference(tmp_path)  # the eval'd output is unchanged
    assert "warning" not in out.stdout


def test_the_old_minimum_is_warned_about_now_that_the_api_has_a_limit(tmp_path: Path) -> None:
    """4.5 GiB predates the API's memory limit (#220): it no longer holds the limits."""
    out = run(tmp_path, f"echo {4608 * MIB}")
    assert "warning: the Docker VM has 4608 MiB" in out.stderr


def test_a_vm_at_or_above_the_minimum_is_not_warned_about(tmp_path: Path) -> None:
    for mib in (5120, 8192):
        out = run(tmp_path, f"echo {mib * MIB}")
        assert out.returncode == 0 and out.stderr == "", (mib, out.stderr)
        assert out.stdout == reference(tmp_path)


@pytest.mark.parametrize(
    "docker",
    [None, "exit 1", "echo not-a-number", "echo", "echo 12abc"],
    ids=["missing", "failing", "garbage", "empty", "mixed"],
)
def test_without_a_usable_answer_from_docker_it_is_silent_and_unchanged(
    tmp_path: Path, docker: str | None
) -> None:
    out = run(tmp_path, docker)
    assert out.returncode == 0, out.stderr
    assert "warning" not in out.stderr
    assert out.stdout == reference(tmp_path)


def test_the_check_can_be_skipped(tmp_path: Path) -> None:
    out = run(tmp_path, f"echo {1024 * MIB}", QUANTLAB_SKIP_VM_CHECK="1")
    assert out.returncode == 0 and out.stderr == ""
    assert out.stdout == reference(tmp_path)


def test_the_worker_count_is_never_changed(tmp_path: Path) -> None:
    """A warning only: B (a worker count following the VM) was decided against."""
    out = run(tmp_path, f"echo {1024 * MIB}")
    assert "QUANTLAB_WORKERS" not in out.stdout
