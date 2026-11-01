"""A bare `docker compose` in a linked worktree never targets the main checkout's project
(#233). It used the default, "quantlab", which is the main checkout's stack and volumes;
three times a command or script reached it. scripts/stack-env.sh, which every `make` runs,
writes a gitignored docker-compose.override.yml naming the worktree's own project, and
writes nothing in the main checkout.

Each test builds a throwaway repository with a linked worktree under tmp_path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = "name: ${COMPOSE_PROJECT_NAME:-quantlab}\nservices:\n  probe:\n    image: busybox\n"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def checkouts(tmp_path: Path) -> tuple[Path, Path]:
    """A main checkout with the stack files, and a linked worktree of it."""
    main = tmp_path / "QuantLab"
    (main / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "stack-env.sh", main / "scripts" / "stack-env.sh")
    shutil.copy(ROOT / ".gitignore", main / ".gitignore")
    (main / "docker-compose.yml").write_text(COMPOSE)
    _git(main, "init", "-q", "-b", "main")
    _git(main, "-c", "user.email=t@example.com", "-c", "user.name=t", "add", ".")
    _git(main, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "x")
    worktree = tmp_path / "agent-9-probe"
    _git(main, "worktree", "add", "-q", str(worktree))
    return main, worktree


def _stack_env(cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
    full = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), **env}
    full["QUANTLAB_SKIP_VM_CHECK"] = "1"
    return subprocess.run(
        ["sh", "scripts/stack-env.sh"], cwd=cwd, env=full, capture_output=True, text=True
    )


def test_a_linked_worktree_gets_an_override_naming_its_own_project(checkouts) -> None:
    _, worktree = checkouts
    out = _stack_env(worktree)
    assert out.returncode == 0, out.stderr
    override = worktree / "docker-compose.override.yml"
    assert override.read_text().splitlines()[-1] == "name: quantlab-agent-9-probe"
    assert "COMPOSE_PROJECT_NAME=quantlab-agent-9-probe" in out.stdout  # eval'd output unchanged


def test_whatever_slot_the_worktree_uses(checkouts) -> None:
    """Even QUANTLAB_SLOT=0 (the main checkout's ports) leaves a bare compose on its own
    project: make exports COMPOSE_PROJECT_NAME, which wins over the file, for that case."""
    _, worktree = checkouts
    assert _stack_env(worktree, QUANTLAB_SLOT="0").returncode == 0
    assert (
        (worktree / "docker-compose.override.yml")
        .read_text()
        .endswith("name: quantlab-agent-9-probe\n")
    )


def test_the_main_checkout_is_unchanged(checkouts) -> None:
    main, _ = checkouts
    out = _stack_env(main)
    assert out.returncode == 0, out.stderr
    assert not (main / "docker-compose.override.yml").exists()
    assert "COMPOSE_PROJECT_NAME=quantlab\n" in out.stdout


def test_the_override_is_gitignored_and_rewritten_only_on_change(checkouts) -> None:
    _, worktree = checkouts
    _stack_env(worktree)
    override = worktree / "docker-compose.override.yml"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=worktree, capture_output=True, text=True
    )
    assert status.stdout == ""  # ignored, nothing to commit
    before = override.stat().st_mtime_ns
    _stack_env(worktree)
    assert override.stat().st_mtime_ns == before


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI (no daemon)")
def test_a_bare_compose_in_the_worktree_never_resolves_to_quantlab(checkouts) -> None:
    """The issue's acceptance check: `docker compose config` there names the worktree's
    project, without COMPOSE_PROJECT_NAME in the environment."""
    main, worktree = checkouts
    _stack_env(worktree)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "QUANTLAB_"))}

    def name(cwd: Path) -> str:
        r = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)["name"]

    assert name(worktree) == "quantlab-agent-9-probe"
    assert name(main) == "quantlab"  # the main checkout: unchanged
    # What make exports still wins over the file's name (-p > COMPOSE_PROJECT_NAME > name:).
    env["COMPOSE_PROJECT_NAME"] = "quantlab-lead-trial"
    assert name(worktree) == "quantlab-lead-trial"
