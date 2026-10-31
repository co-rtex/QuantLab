"""A long upload re-checks the session after its body is read (#8, PR C).

Authentication runs before the body (#4), and a large upload can take minutes. If the
account is deactivated, or its sessions are revoked with "log out everywhere", meanwhile,
the upload must not still create a job.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, update

from tests.integration.test_upload_auth import (
    _authorized,
    _csv_path,
    drive,
    multipart_body,
    run,
    uploads_dir,  # noqa: F401 - fixture
)


@pytest.mark.parametrize("change", ["deactivated", "logged_out_everywhere"])
def test_session_revoked_mid_upload_is_rejected(client, uploads_dir, change):  # noqa: F811
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Job, User

    h, ws = _authorized(client)
    me = uuid.UUID(client.get("/api/v1/auth/me", headers=h).json()["id"])
    values = {"is_active": False} if change == "deactivated" else {"token_version": 1}

    def revoke(call: int) -> None:
        if call == 2:  # the body is still streaming
            with SessionLocal() as db:
                db.execute(update(User).where(User.id == me).values(**values))
                db.commit()

    total, chunks = multipart_body(2**20)
    res = run(drive(_csv_path(ws), chunks, headers=h, content_length=total, on_pull=revoke))
    assert res.status == 401, res.body
    assert res.json()["error"]["code"] == "invalid_token"
    assert uploads_dir.new() == set()
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_an_unchanged_session_still_uploads(client, uploads_dir):  # noqa: F811
    h, ws = _authorized(client)
    total, chunks = multipart_body(2**20)
    res = run(drive(_csv_path(ws), chunks, headers=h, content_length=total))
    assert res.status == 202, res.body
