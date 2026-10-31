"""Consent-based workspace membership through invitations (#5)."""

from __future__ import annotations

import statistics
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, select

from tests.integration.conftest import invitation_token, join_workspace, register, workspace_id

ACCEPT = "/api/v1/auth/invitations/accept"
DECLINE = "/api/v1/auth/invitations/decline"
PREVIEW = "/api/v1/auth/invitations/preview"


def _invite(client, owner, ws, email, role="viewer"):  # type: ignore[no-untyped-def]
    return client.post(
        f"/api/v1/workspaces/{ws}/invitations", headers=owner, json={"email": email, "role": role}
    )


def _members(client, owner, ws) -> dict[str, str]:  # type: ignore[no-untyped-def]
    r = client.get(f"/api/v1/workspaces/{ws}/members", headers=owner)
    return {m["email"]: m["role"] for m in r.json()}


@pytest.fixture
def team(client):  # type: ignore[no-untyped-def]
    owner = register(client, "owner@example.com")
    ws = workspace_id(client, owner)
    bob = register(client, "bob@example.com")
    return {"owner": owner, "ws": ws, "bob": bob}


# ------------------------------------------------------------------- enumeration
def test_invite_response_is_identical_for_registered_and_unknown_addresses(client, team):
    known = _invite(client, team["owner"], team["ws"], "bob@example.com")
    unknown = _invite(client, team["owner"], team["ws"], "nobody@example.com")
    assert known.status_code == unknown.status_code == 201

    def shape(r):  # type: ignore[no-untyped-def]
        body = r.json()
        return {
            k: v for k, v in body.items() if k not in {"id", "email", "created_at", "expires_at"}
        }

    assert shape(known) == shape(unknown) == {"role": "viewer", "status": "pending"}
    assert set(known.json()) == set(unknown.json())
    assert dict(known.headers).keys() == dict(unknown.headers).keys()


def test_creating_an_invitation_never_reads_the_users_table(client, team):
    """The structural guarantee behind uniform timing: no query depends on whether the
    address has an account."""
    from quantlab_api.db import SessionLocal

    with SessionLocal() as probe:
        engine = probe.get_bind()
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    sequences = []
    try:
        for email in ("bob@example.com", "nobody@example.com"):
            statements.clear()
            sequences.append(statements)
            assert _invite(client, team["owner"], team["ws"], email).status_code == 201
            # Authentication reads the *inviter's* row by primary key; nothing else may.
            users = [" ".join(s.split()) for s in statements if "users" in s]
            assert len(users) == 1, users
            assert users[0].endswith("FROM users WHERE users.id = %(pk_1)s::UUID"), users
            statements = []
    finally:
        event.remove(engine, "before_cursor_execute", record)
    # Same statements, in the same order, for a registered and an unknown address.
    assert sequences[0] == sequences[1]


def test_invite_timing_does_not_depend_on_registration(client, team, monkeypatch):
    from quantlab_api.routes import invitations as routes

    monkeypatch.setattr(routes, "INVITES_PER_HOUR_PER_INVITER", 1000)
    monkeypatch.setattr(routes, "INVITES_PER_HOUR_PER_WORKSPACE", 1000)

    def median_ms(email_for) -> float:  # type: ignore[no-untyped-def]
        samples = []
        for i in range(15):
            t0 = time.perf_counter()
            assert _invite(client, team["owner"], team["ws"], email_for(i)).status_code == 201
            samples.append((time.perf_counter() - t0) * 1000)
        return statistics.median(samples)

    # Same number of registered and unknown targets, interleaved to share any drift.
    for i in range(15):
        register(client, f"known{i}@example.com")
    known = median_ms(lambda i: f"known{i}@example.com")
    unknown = median_ms(lambda i: f"unknown{i}@example.com")
    assert abs(known - unknown) < max(5.0, 0.25 * min(known, unknown)), (known, unknown)


# ----------------------------------------------------------------------- consent
def test_membership_exists_only_after_the_invitee_accepts(client, team):
    assert (
        _invite(client, team["owner"], team["ws"], "bob@example.com", "editor").status_code == 201
    )
    assert "bob@example.com" not in _members(client, team["owner"], team["ws"])
    bob_ws = client.get(f"/api/v1/workspaces/{team['ws']}", headers=team["bob"])
    assert bob_ws.status_code == 404
    token = invitation_token(client, team["bob"])
    preview = client.post(PREVIEW, headers=team["bob"], json={"token": token})
    assert preview.status_code == 200
    assert preview.json()["role"] == "editor"
    assert preview.json()["invited_by"] == "owner"
    r = client.post(ACCEPT, headers=team["bob"], json={"token": token})
    assert r.status_code == 200, r.text
    assert _members(client, team["owner"], team["ws"])["bob@example.com"] == "editor"


def test_declined_invitation_adds_no_member_and_cannot_be_accepted(client, team):
    _invite(client, team["owner"], team["ws"], "bob@example.com")
    token = invitation_token(client, team["bob"])
    assert client.post(DECLINE, headers=team["bob"], json={"token": token}).status_code == 204
    again = client.post(ACCEPT, headers=team["bob"], json={"token": token})
    assert again.status_code == 409
    assert "bob@example.com" not in _members(client, team["owner"], team["ws"])


def test_direct_add_member_is_gone(client, team):
    r = client.post(
        f"/api/v1/workspaces/{team['ws']}/members",
        headers=team["owner"],
        json={"email": "bob@example.com", "role": "viewer"},
    )
    assert r.status_code == 405


# ------------------------------------------------------------ token lifecycle
def test_token_is_single_use(client, team):
    _invite(client, team["owner"], team["ws"], "bob@example.com")
    token = invitation_token(client, team["bob"])
    assert client.post(ACCEPT, headers=team["bob"], json={"token": token}).status_code == 200
    replay = client.post(ACCEPT, headers=team["bob"], json={"token": token})
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "invitation_used"


def test_expired_token_is_rejected(client, team):
    from sqlalchemy import update

    from quantlab_api.db import SessionLocal
    from quantlab_api.invitations import Invitation

    _invite(client, team["owner"], team["ws"], "bob@example.com")
    token = invitation_token(client, team["bob"])
    with SessionLocal() as db:
        db.execute(update(Invitation).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        db.commit()
    for path in (PREVIEW, ACCEPT, DECLINE):
        r = client.post(path, headers=team["bob"], json={"token": token})
        assert r.status_code == 410, (path, r.text)
    listed = client.get(f"/api/v1/workspaces/{team['ws']}/invitations", headers=team["owner"])
    assert listed.json()[0]["status"] == "expired"
    assert "bob@example.com" not in _members(client, team["owner"], team["ws"])


def test_revoked_token_is_rejected(client, team):
    inv = _invite(client, team["owner"], team["ws"], "bob@example.com").json()
    token = invitation_token(client, team["bob"])
    revoke = client.delete(
        f"/api/v1/workspaces/{team['ws']}/invitations/{inv['id']}", headers=team["owner"]
    )
    assert revoke.status_code == 204
    r = client.post(ACCEPT, headers=team["bob"], json={"token": token})
    assert r.status_code == 409
    assert "bob@example.com" not in _members(client, team["owner"], team["ws"])


def test_reinvite_rotates_the_token(client, team):
    _invite(client, team["owner"], team["ws"], "bob@example.com", "viewer")
    old = invitation_token(client, team["bob"])
    _invite(client, team["owner"], team["ws"], "bob@example.com", "editor")
    new = invitation_token(client, team["bob"])
    assert old != new
    assert client.post(ACCEPT, headers=team["bob"], json={"token": old}).status_code == 409
    assert client.post(ACCEPT, headers=team["bob"], json={"token": new}).status_code == 200
    assert _members(client, team["owner"], team["ws"])["bob@example.com"] == "editor"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t[:-1] + ("A" if t[-1] != "A" else "B"),
        lambda t: t[1:] + "x",
        lambda t: t.upper(),
        lambda t: "x" * 43,
    ],
)
def test_tampered_tokens_are_rejected(client, team, mutate):
    _invite(client, team["owner"], team["ws"], "bob@example.com")
    token = invitation_token(client, team["bob"])
    r = client.post(ACCEPT, headers=team["bob"], json={"token": mutate(token)})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "invitation_not_found"
    assert client.post(ACCEPT, headers=team["bob"], json={"token": token}).status_code == 200


def test_token_for_another_account_reveals_nothing(client, team):
    """Registration does not verify email ownership, so the token alone is not enough:
    the signed-in account must be the invited address."""
    _invite(client, team["owner"], team["ws"], "bob@example.com")
    token = invitation_token(client, team["bob"])
    mallory = register(client, "mallory@example.com")
    for path in (PREVIEW, ACCEPT, DECLINE):
        r = client.post(path, headers=mallory, json={"token": token})
        assert r.status_code == 404, path
        assert "workspace" not in r.text.lower() or "owner" not in r.text
    assert "mallory@example.com" not in _members(client, team["owner"], team["ws"])
    # Bob can still accept: Mallory's attempts did not consume the invitation.
    assert client.post(ACCEPT, headers=team["bob"], json={"token": token}).status_code == 200


def test_unauthenticated_invitee_endpoints_require_login(client, team):
    for path in (PREVIEW, ACCEPT, DECLINE):
        assert client.post(path, json={"token": "x" * 43}).status_code == 401
    assert client.get("/api/v1/auth/invitations/dev-mailbox").status_code == 401


def test_email_match_is_case_insensitive(client, team):
    assert _invite(client, team["owner"], team["ws"], "Bob@Example.COM").status_code == 201
    token = invitation_token(client, team["bob"])
    assert client.post(ACCEPT, headers=team["bob"], json={"token": token}).status_code == 200


def test_tokens_are_stored_only_as_hashes(client, team):
    from quantlab_api.db import SessionLocal
    from quantlab_api.invitations import Invitation, hash_token
    from quantlab_api.models import AuditEvent

    created = _invite(client, team["owner"], team["ws"], "bob@example.com")
    token = invitation_token(client, team["bob"])
    assert token not in created.text
    assert len(token) >= 43  # 256 bits, base64url
    with SessionLocal() as db:
        inv = db.scalar(select(Invitation))
        assert inv is not None and inv.token_hash == hash_token(token) != token
        details = [str(a.details) for a in db.scalars(select(AuditEvent))]
    assert not any(token in d for d in details)


def test_concurrent_accepts_admit_exactly_once(client, team):
    from fastapi.testclient import TestClient

    from quantlab_api.db import SessionLocal
    from quantlab_api.main import app
    from quantlab_api.models import WorkspaceMember

    _invite(client, team["owner"], team["ws"], "bob@example.com", "editor")
    token = invitation_token(client, team["bob"])
    codes: list[int] = []
    barrier = threading.Barrier(8)

    def go() -> None:
        with TestClient(app) as c:
            barrier.wait()
            codes.append(c.post(ACCEPT, headers=team["bob"], json={"token": token}).status_code)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [200] + [409] * 7, codes
    with SessionLocal() as db:
        rows = db.scalars(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id == uuid.UUID(team["ws"]))
        ).all()
    assert len(rows) == 2  # owner + bob, once


# ------------------------------------------------------------------ authorization
def test_authorization_matrix(client, team):
    viewer = register(client, "viewer@example.com")
    editor = register(client, "editor@example.com")
    outsider = register(client, "outsider@example.com")
    join_workspace(client, team["owner"], team["ws"], viewer, "viewer@example.com", "viewer")
    join_workspace(client, team["owner"], team["ws"], editor, "editor@example.com", "editor")
    inv = _invite(client, team["owner"], team["ws"], "x@example.com").json()
    base = f"/api/v1/workspaces/{team['ws']}/invitations"
    for who, expected in ((outsider, 404), (viewer, 403), (editor, 403)):
        assert _invite(client, who, team["ws"], "y@example.com").status_code == expected
        assert client.get(base, headers=who).status_code == expected
        assert client.delete(f"{base}/{inv['id']}", headers=who).status_code == expected
        bob_id = client.get("/api/v1/auth/me", headers=team["bob"]).json()["id"]
        patch = client.patch(
            f"/api/v1/workspaces/{team['ws']}/members/{bob_id}", headers=who, json={"role": "owner"}
        )
        assert patch.status_code == expected
    assert client.get(base, headers=team["owner"]).status_code == 200


def test_owner_cannot_revoke_another_workspaces_invitation(client, team):
    other = register(client, "other-owner@example.com")
    other_ws = workspace_id(client, other)
    inv = _invite(client, other, other_ws, "bob@example.com").json()
    # Owner of team["ws"] uses its own workspace id with a foreign invitation id.
    r = client.delete(
        f"/api/v1/workspaces/{team['ws']}/invitations/{inv['id']}", headers=team["owner"]
    )
    assert r.status_code == 404
    token = invitation_token(client, team["bob"])
    assert client.post(ACCEPT, headers=team["bob"], json={"token": token}).status_code == 200


def test_invitation_raises_but_never_lowers_a_role(client, team):
    join_workspace(client, team["owner"], team["ws"], team["bob"], "bob@example.com", "editor")
    _invite(client, team["owner"], team["ws"], "bob@example.com", "viewer")
    client.post(ACCEPT, headers=team["bob"], json={"token": invitation_token(client, team["bob"])})
    assert _members(client, team["owner"], team["ws"])["bob@example.com"] == "editor"
    _invite(client, team["owner"], team["ws"], "bob@example.com", "owner")
    client.post(ACCEPT, headers=team["bob"], json={"token": invitation_token(client, team["bob"])})
    assert _members(client, team["owner"], team["ws"])["bob@example.com"] == "owner"


def test_role_changes_keep_an_owner(client, team):
    join_workspace(client, team["owner"], team["ws"], team["bob"], "bob@example.com", "owner")
    me = client.get("/api/v1/auth/me", headers=team["owner"]).json()["id"]
    bob = client.get("/api/v1/auth/me", headers=team["bob"]).json()["id"]
    url = f"/api/v1/workspaces/{team['ws']}/members"
    assert (
        client.patch(f"{url}/{me}", headers=team["owner"], json={"role": "viewer"}).status_code
        == 200
    )
    last = client.patch(f"{url}/{bob}", headers=team["bob"], json={"role": "editor"})
    assert last.status_code == 409
    unknown = client.patch(f"{url}/{uuid.uuid4()}", headers=team["bob"], json={"role": "editor"})
    assert unknown.status_code == 404


def test_deleting_a_workspace_removes_its_invitations(client, team):
    from quantlab_api.db import SessionLocal
    from quantlab_api.invitations import Invitation

    created = client.post("/api/v1/workspaces", headers=team["owner"], json={"name": "Temp"})
    temp = created.json()["id"]
    _invite(client, team["owner"], temp, "bob@example.com")
    assert client.delete(f"/api/v1/workspaces/{temp}", headers=team["owner"]).status_code == 204
    with SessionLocal() as db:
        assert (
            db.scalar(select(Invitation).where(Invitation.workspace_id == uuid.UUID(temp))) is None
        )
    token = invitation_token(client, team["bob"])
    assert client.post(ACCEPT, headers=team["bob"], json={"token": token}).status_code == 404


# ---------------------------------------------------------------- abuse bounds
def test_invitations_are_rate_limited_per_inviter(client, team):
    from quantlab_api.invitations import INVITES_PER_HOUR_PER_INVITER

    codes = [
        _invite(client, team["owner"], team["ws"], f"r{i}@example.com").status_code
        for i in range(INVITES_PER_HOUR_PER_INVITER + 1)
    ]
    assert codes[:-1] == [201] * INVITES_PER_HOUR_PER_INVITER
    assert codes[-1] == 429


def test_pending_invitations_are_capped_per_workspace(client, team, monkeypatch):
    from quantlab_api import invitations
    from quantlab_api.routes import invitations as routes

    monkeypatch.setattr(routes, "MAX_PENDING_PER_WORKSPACE", 3)
    monkeypatch.setattr(invitations, "MAX_PENDING_PER_WORKSPACE", 3)
    codes = [
        _invite(client, team["owner"], team["ws"], f"p{i}@example.com").status_code
        for i in range(4)
    ]
    assert codes == [201, 201, 201, 429]
    # Re-inviting a pending address replaces it rather than counting twice.
    assert _invite(client, team["owner"], team["ws"], "p0@example.com").status_code == 201


def test_delivery_failure_is_reported_and_the_invitation_can_be_resent(client, team, monkeypatch):
    from quantlab_api import invitations

    class Broken:
        def send_invitation(self, message: Any) -> None:
            raise ConnectionError("mail relay down")

    real = invitations.notifier
    monkeypatch.setattr(invitations, "notifier", Broken())
    r = _invite(client, team["owner"], team["ws"], "bob@example.com")
    assert r.status_code == 502
    monkeypatch.setattr(invitations, "notifier", real)
    assert _invite(client, team["owner"], team["ws"], "bob@example.com").status_code == 201
    token = invitation_token(client, team["bob"])
    assert client.post(ACCEPT, headers=team["bob"], json={"token": token}).status_code == 200


# -------------------------------------------------------------- local delivery
def test_dev_mailbox_shows_only_your_own_messages(client, team):
    _invite(client, team["owner"], team["ws"], "bob@example.com")
    _invite(client, team["owner"], team["ws"], "carol@example.com")
    mail = client.get("/api/v1/auth/invitations/dev-mailbox", headers=team["bob"]).json()
    assert [m["to"] for m in mail] == ["bob@example.com"]
    assert mail[0]["accept_path"].startswith("/invite#token=")
    owner_mail = client.get("/api/v1/auth/invitations/dev-mailbox", headers=team["owner"])
    assert owner_mail.json() == []


def test_dev_mailbox_is_disabled_in_production(client, team, monkeypatch):
    from quantlab_api.config import get_settings

    _invite(client, team["owner"], team["ws"], "bob@example.com")
    monkeypatch.setattr(get_settings(), "environment", "production")
    r = client.get("/api/v1/auth/invitations/dev-mailbox", headers=team["bob"])
    assert r.status_code == 404


def test_outbox_files_are_private(client, team):
    from quantlab_api.config import get_settings

    _invite(client, team["owner"], team["ws"], "bob@example.com")
    outbox = get_settings().data_dir / "outbox"
    assert outbox.stat().st_mode & 0o077 == 0
    for f in outbox.iterdir():
        assert f.stat().st_mode & 0o077 == 0


def test_model_module_is_safe_for_alembic_to_import():
    """alembic/env.py imports the model before the API creates the Prometheus multiprocess
    directory; importing quantlab_api.observability then crashed every container start."""
    import subprocess
    import sys

    code = (
        "import sys, quantlab_api.invitations; "
        "sys.exit(1 if 'quantlab_api.observability' in sys.modules else 0)"
    )
    env = {"PROMETHEUS_MULTIPROC_DIR": "/nonexistent/prometheus", "PATH": ""}
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
