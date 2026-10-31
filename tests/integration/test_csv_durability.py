"""CSV imports survive failed and ambiguous commits (#7).

Staged upload files may be deleted only once it is known that no committed job still
needs them. The finalizer deleted them before its transaction committed, so a failed
commit turned a retryable error into "uploaded files are no longer available". The upload
route deleted them whenever the request failed, even when the failure came after the
job had been committed.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from tests.integration.conftest import register, run_pending_jobs, workspace_id
from tests.integration.test_csv_import import _upload


@pytest.fixture
def staged_dirs():  # type: ignore[no-untyped-def]
    from quantlab_api.config import get_settings

    root = get_settings().data_dir / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    before = set(root.iterdir())
    yield lambda: set(root.iterdir()) - before
    # Files these tests deliberately kept would otherwise leak into later tests.
    import shutil

    for d in set(root.iterdir()) - before:
        shutil.rmtree(d, ignore_errors=True)


def test_a_failed_commit_keeps_the_files_and_the_retry_succeeds(client, staged_dirs):
    h = register(client)
    ws = workspace_id(client, h)
    assert _upload(client, h, ws).status_code == 202
    (staged,) = staged_dirs()

    failed: list[bool] = []

    def fail_first_job_commit(session: Session) -> None:
        # Only the worker's finalize transaction marks a job completed.
        from quantlab_api.models import Job, JobStatus

        if not failed and any(
            isinstance(o, Job) and o.status == JobStatus.completed for o in session.dirty
        ):
            failed.append(True)
            raise ConnectionError("connection lost during COMMIT")

    event.listen(Session, "before_commit", fail_first_job_commit)
    try:
        assert run_pending_jobs(max_jobs=1) == ["retry"]
    finally:
        event.remove(Session, "before_commit", fail_first_job_commit)
    assert failed == [True]
    assert staged.exists()  # still needed by the retry
    assert run_pending_jobs() == ["completed"]
    assert not staged.exists()  # released once the version is committed
    versions = client.get(f"/api/v1/workspaces/{ws}/dataset-versions", headers=h).json()
    assert len(versions) == 1


def test_an_ambiguous_upload_commit_keeps_the_files_the_job_needs(client, monkeypatch, staged_dirs):
    """The job was committed but the commit call still raised (the connection dropped
    while the server answered). The request fails; the files must stay for the job."""
    from quantlab_api.routes import datasets

    real = datasets.submit_job

    def committed_then_lost(*args: Any, **kwargs: Any) -> Any:
        real(*args, **kwargs)
        raise ConnectionError("connection lost after COMMIT")

    monkeypatch.setattr(datasets, "submit_job", committed_then_lost)
    client_500 = client.__class__(client.app, raise_server_exceptions=False)
    h = register(client_500)
    ws = workspace_id(client_500, h)
    r = _upload(client_500, h, ws)
    assert r.status_code == 500
    (staged,) = staged_dirs()
    assert staged.exists()
    monkeypatch.setattr(datasets, "submit_job", real)
    assert run_pending_jobs() == ["completed"]
    assert not staged.exists()


def test_when_the_outcome_cannot_be_checked_the_files_stay(client, monkeypatch, staged_dirs):
    """Commit outcome unknown and the database unreachable for the check: keep the files
    (the reaper sweeps them after 24 h) rather than break a job that may exist."""
    from quantlab_api import db as dbmod
    from quantlab_api.routes import datasets

    real = datasets.submit_job

    def lost(*args: Any, **kwargs: Any) -> Any:
        real(*args, **kwargs)
        monkeypatch.setattr(dbmod, "SessionLocal", _unreachable)
        raise ConnectionError("connection lost after COMMIT")

    def _unreachable() -> Any:
        raise ConnectionError("database unreachable")

    monkeypatch.setattr(datasets, "submit_job", lost)
    client_500 = client.__class__(client.app, raise_server_exceptions=False)
    h = register(client_500)
    ws = workspace_id(client_500, h)
    assert _upload(client_500, h, ws).status_code == 500
    (staged,) = staged_dirs()
    assert staged.exists()


def test_a_rejected_upload_still_releases_its_files(client, monkeypatch, staged_dirs):
    """Nothing was committed: the files go, as before."""
    from quantlab_api.config import get_settings

    monkeypatch.setattr(get_settings(), "max_active_jobs_per_workspace", 0)
    h = register(client)
    ws = workspace_id(client, h)
    assert _upload(client, h, ws).status_code == 429
    assert staged_dirs() == set()
