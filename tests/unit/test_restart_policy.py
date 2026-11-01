"""Which compose services Docker restarts (#162).

The core services restart unless stopped, so a crashed or OOM-killed worker comes back. A
linked worktree's dev stack does not (scripts/stack-env.sh sets "no"), so a Docker restart
does not start every agent's stack at once in the shared VM. The edge overlay, the exposed
deployment, always restarts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE = {"api", "worker", "reaper", "web", "postgres", "redis"}


def stack_env(**env: str) -> dict[str, str]:
    """scripts/stack-env.sh's settings for a clean environment plus ``env``."""
    out = subprocess.run(
        ["sh", "scripts/stack-env.sh"],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), **env},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines())


def _compose() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


needs_compose = pytest.mark.skipif(not _compose(), reason="needs the docker compose CLI")


def restart(env: dict[str, str], *files: str) -> dict[str, str]:
    """Each app-profile service's resolved restart policy (no daemon needed)."""
    args = [a for f in files or ("docker-compose.yml",) for a in ("-f", f)]
    full = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), **env}
    out = subprocess.run(
        ["docker", "compose", *args, "--profile", "app", "config", "--format", "json"],
        cwd=ROOT,
        env=full,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {k: v.get("restart") for k, v in json.loads(out)["services"].items()}


def test_stack_env_sets_no_for_a_worktree_and_unless_stopped_for_the_main_checkout() -> None:
    assert stack_env(QUANTLAB_SLOT="0")["QUANTLAB_RESTART_POLICY"] == "unless-stopped"
    assert stack_env(QUANTLAB_SLOT="21")["QUANTLAB_RESTART_POLICY"] == "no"
    explicit = stack_env(QUANTLAB_SLOT="21", QUANTLAB_RESTART_POLICY="unless-stopped")
    assert explicit["QUANTLAB_RESTART_POLICY"] == "unless-stopped"


@needs_compose
def test_the_main_checkout_restarts_its_core_services() -> None:
    got = restart(stack_env(QUANTLAB_SLOT="0"))
    assert {got[s] for s in CORE} == {"unless-stopped"}


@needs_compose
def test_a_worktree_slot_does_not_restart_its_stack() -> None:
    got = restart(stack_env(QUANTLAB_SLOT="21"))
    assert {got[s] for s in CORE} == {"no"}


@needs_compose
def test_an_explicit_policy_wins_for_a_crash_recovery_probe() -> None:
    got = restart(stack_env(QUANTLAB_SLOT="21", QUANTLAB_RESTART_POLICY="unless-stopped"))
    assert {got[s] for s in CORE} == {"unless-stopped"}


@needs_compose
def test_without_stack_env_the_default_is_unless_stopped() -> None:
    assert {restart({})[s] for s in CORE} == {"unless-stopped"}


@needs_compose
def test_the_edge_overlay_always_restarts_even_from_a_worktree_slot() -> None:
    env = {**stack_env(QUANTLAB_SLOT="21"), "QUANTLAB_EDGE_SECRET": "x" * 64}
    got = restart(env, "docker-compose.yml", "infra/edge/compose.yml")
    assert {got[s] for s in CORE | {"edge"}} == {"unless-stopped"}
