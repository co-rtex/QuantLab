"""Every run records the code it ran, and an unrecorded version is never "the same" (A21).

Before, nothing set QUANTLAB_GIT_SHA, so every containerized experiment recorded the code
version "unknown", and a reproduction compared "unknown" with "unknown" as the same code.
scripts/stack-env.sh now emits the checkout's commit ("-dirty" with uncommitted changes),
compose bakes it into the image as a build argument, and the comparison treats an unknown
or dirty version as not comparable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from quantlab.research.integrity import build_integrity_report
from quantlab_api.routes.experiments import same_code_version

ROOT = Path(__file__).resolve().parents[2]


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _sha(cwd: Path, **env: str) -> str:
    full = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp"), **env}
    full["QUANTLAB_SKIP_VM_CHECK"] = "1"
    out = subprocess.run(
        ["sh", str(cwd / "scripts" / "stack-env.sh")],
        cwd=cwd,
        env=full,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (line,) = [x for x in out.splitlines() if x.startswith("QUANTLAB_GIT_SHA=")]
    return line.split("=", 1)[1]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    top = tmp_path / "QuantLab"
    (top / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "stack-env.sh", top / "scripts" / "stack-env.sh")
    (top / "tracked.txt").write_text("a\n")
    _git(top, "init", "-q", "-b", "main")
    _git(top, "add", ".")
    _git(top, "commit", "-q", "-m", "x")
    return top


def test_a_clean_checkout_reports_its_commit(repo: Path) -> None:
    assert _sha(repo) == _git(repo, "rev-parse", "HEAD")


def test_uncommitted_tracked_changes_mark_it_dirty_and_untracked_files_do_not(
    repo: Path,
) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "scratch.png").write_text("test output")  # untracked: still clean
    assert _sha(repo) == head
    (repo / "tracked.txt").write_text("b\n")
    assert _sha(repo) == f"{head}-dirty"


def test_outside_git_it_is_unknown_and_an_explicit_value_wins(tmp_path: Path) -> None:
    top = tmp_path / "export"
    (top / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "stack-env.sh", top / "scripts" / "stack-env.sh")
    assert _sha(top) == "unknown"
    assert _sha(top, QUANTLAB_GIT_SHA="abc123") == "abc123"


def test_compose_bakes_it_into_the_image_and_never_overrides_it_at_runtime() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    # A runtime value would replace the image's own with whatever the shell had (or
    # "unknown") whenever a stack is started from an older image.
    assert "QUANTLAB_GIT_SHA" not in compose["x-app-env"]
    for name in ("api", "worker", "reaper"):
        service = compose["services"][name]
        assert service["build"]["args"]["QUANTLAB_GIT_SHA"] == "${QUANTLAB_GIT_SHA:-unknown}"
        assert "QUANTLAB_GIT_SHA" not in (service.get("environment") or {})
    dockerfile = (ROOT / "services" / "api" / "Dockerfile").read_text().splitlines()
    arg = dockerfile.index("ARG QUANTLAB_GIT_SHA=unknown")
    assert dockerfile[arg + 1] == "ENV QUANTLAB_GIT_SHA=$QUANTLAB_GIT_SHA"
    # Declared after every COPY and RUN, so a new commit reuses all the layers above.
    assert not any(line.startswith(("COPY", "RUN", "ADD")) for line in dockerfile[arg:])


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("abc", "abc", True),
        ("abc", "def", False),
        ("unknown", "unknown", None),
        ("abc", "unknown", None),
        ("abc-dirty", "abc-dirty", None),
        ("abc", "abc-dirty", None),
        ("", "abc", None),
        (None, None, None),
    ],
)
def test_only_two_recorded_clean_versions_are_compared(a, b, same) -> None:  # type: ignore[no-untyped-def]
    assert same_code_version(a, b) is same


def _report(*reruns: dict) -> str:  # type: ignore[type-arg]
    evidence = {
        "experiment": {"fingerprint": "f", "input_hash": "i", "code_version": "unknown"},
        "dataset": {"version_hash": "v", "is_synthetic": True},
        "reproductions": list(reruns),
    }
    by = {c["key"]: c for c in build_integrity_report(evidence)["checks"]}
    assert by["reproducibility"]["status"] == "verified"
    return str(by["reproducibility"]["finding"])


def test_the_report_says_when_identical_outputs_came_from_uncomparable_code() -> None:
    rerun = {"status": "completed", "same_inputs": True, "fingerprint_match": True}
    clean = _report({**rerun, "same_code_version": True})
    assert clean == "1 independent re-run produced byte-identical results."
    blind = _report({**rerun, "same_code_version": None}, {**rerun, "same_code_version": True})
    assert blind.startswith("2 independent re-runs produced byte-identical results.")
    assert "could not be compared for 1 of them" in blind
