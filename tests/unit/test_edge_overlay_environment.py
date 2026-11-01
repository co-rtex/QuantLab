"""The edge overlay always runs the API, worker and reaper in production (#235).

Exposure means production: the production guards (no API docs, no dev JWT secret, the
trusted-peer checks) must not depend on the operator's shell or .env, where
``.env.example`` itself sets ``QUANTLAB_ENVIRONMENT=development``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EDGE = ("docker-compose.yml", "infra/edge/compose.yml")
EXPOSED = ("api", "worker", "reaper")
RENDER = ("--profile", "app", "config", "--format", "json")


def _compose() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


needs_compose = pytest.mark.skipif(not _compose(), reason="needs the docker compose CLI")


def test_the_overlay_pins_production_with_no_substitution() -> None:
    # Compose's own "!reset" tag is not YAML's; the values under it are plain.
    overlay = yaml.safe_load((ROOT / EDGE[1]).read_text().replace("!reset ", ""))
    services: dict[str, Any] = overlay["services"]
    for name in EXPOSED:
        assert services[name]["environment"]["QUANTLAB_ENVIRONMENT"] == "production", name


def resolved_environment(files: tuple[str, ...], **env: str) -> dict[str, str]:
    """Each exposed service's QUANTLAB_ENVIRONMENT as compose renders it (no daemon)."""
    full = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        # Its own project name, so the render never names another checkout's stack.
        "COMPOSE_PROJECT_NAME": "quantlab-edge-env-check",
        "QUANTLAB_EDGE_SECRET": "x" * 64,
        **env,
    }
    out = subprocess.run(
        ["docker", "compose", *(a for f in files for a in ("-f", f)), *RENDER],
        cwd=ROOT,
        env=full,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    services = json.loads(out)["services"]
    return {name: services[name]["environment"]["QUANTLAB_ENVIRONMENT"] for name in EXPOSED}


@needs_compose
@pytest.mark.parametrize("value", ["development", "test", ""])
def test_an_exported_environment_never_turns_off_production_behind_the_edge(
    value: str,
) -> None:
    got = resolved_environment(EDGE, QUANTLAB_ENVIRONMENT=value)
    assert got == dict.fromkeys(EXPOSED, "production")


@needs_compose
def test_without_the_overlay_the_dev_stack_keeps_its_development_default() -> None:
    got = resolved_environment(EDGE[:1])
    assert got == dict.fromkeys(EXPOSED, "development")
