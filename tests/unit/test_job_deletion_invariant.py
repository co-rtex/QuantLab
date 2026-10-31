"""ADR-0011: application code never deletes job rows on their own.

`experiment_results.job_id` cascades on delete, so deleting a job would silently delete a
backtest's results. Jobs may only disappear through the workspace cascade.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [ROOT / "services" / "api" / "src", ROOT / "packages" / "quantlab" / "src"]
# delete(Job ...), session.delete(job), DELETE FROM jobs, Job.__table__.delete()
PATTERNS = [
    re.compile(r"\bdelete\(\s*Job\b"),
    re.compile(r"\.delete\(\s*job\b"),
    re.compile(r"DELETE\s+FROM\s+\"?jobs\b", re.IGNORECASE),
    re.compile(r"\bJob\.__table__\.delete\("),
]


def test_no_application_code_deletes_jobs() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{n}: {line.strip()}"
        for src in SOURCES
        for path in sorted(src.rglob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if any(p.search(line) for p in PATTERNS)
    ]
    assert not offenders, (
        "Job rows must not be deleted on their own (ADR-0011): experiment_results cascade "
        "on job deletion. Migrate experiment_results.job_id to SET NULL first.\n"
        + "\n".join(offenders)
    )


def test_the_guard_detects_a_job_delete() -> None:
    sample = "db.execute(delete(Job).where(Job.id == jid))\nsession.delete(job)\n"
    hits = [line for line in sample.splitlines() if any(p.search(line) for p in PATTERNS)]
    assert len(hits) == 2
