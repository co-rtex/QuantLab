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

from tests.integration.conftest import accept_token, join_workspace, register, workspace_id

ACCEPT = "/api/v1/auth/invitations/accept"
DECLINE = "/api/v1/auth/invitations/decline"
PREVIEW = "/api/v1/auth/invitations/preview"


def _invite(client, owner, ws, email, role="viewer"):  # type: ignore[no-untyped-def]
    return client.post(
        f"/api/v1/workspaces/{ws}/invitations", headers=owner, json={"email": email, "role": role}
    )


def _token(client, owner, ws, email, role="viewer") -> str:  # type: ignore[no-untyped-def]
    r = _invite(client, owner, ws, email, role)
    assert r.status_code == 201, r.text
    return accept_token(r)


def _members(client, owner, ws) -> dict[str, str]:  # type: ignore[no-untyped-def]
    r = client.get(f"/api/v1/workspaces/{ws}/members", headers=owner)
    return {m["email"]: m["role"] for m in r.json()}


def _uid(client, headers) -> str:  # type: ignore[no-untyped-def]
    return str(client.get("/api/v1/auth/me", headers=headers).json()["id"])


@pytest.fixture
def team(client):  # type: ignore[no-untyped-def]
    owner = register(client, "owner@example.com")
    ws = workspace_id(client, owner)
    bob = register(client, "bob@example.com")
    return {"owner": owner, "ws": ws, "bob": bob}


@pytest.fixture
def no_invite_limits(monkeypatch):  # type: ignore[no-untyped-def]
    from quantlab_api.routes import invitations as routes

    for name in (
        "INVITES_PER_HOUR_PER_INVITER",
        "INVITES_PER_HOUR_PER_WORKSPACE",
        "INVITES_PER_HOUR_PER_ADDRESS",
    ):
        monkeypatch.setattr(routes, name, 10_000)


# ------------------------------------------------------------------- enumeration
def test_invite_response_is_identical_for_registered_and_unknown_addresses(client, team):
    known = _invite(client, team["owner"], team["ws"], "bob@example.com")
    unknown = _invite(client, team["owner"], team["ws"], "nobody@example.com")
    assert known.status_code == unknown.status_code == 201
    varying = {"id", "email", "created_at", "expires_at", "accept_path"}

    def shape(r):  # type: ignore[no-untyped-def]
        return {k: v for k, v in r.json().items() if k not in varying}

    assert shape(known) == shape(unknown) == {"role": "viewer", "status": "pending"}
    assert set(known.json()) == set(unknown.json())
    assert dict(known.headers).keys() == dict(unknown.headers).keys()


def test_creating_an_invitation_never_reads_the_users_table(client, team):
    """The structural guarantee behind uniform timing: the same statements run whether or
    not the address has an account, and none of them looks it up."""
    from quantlab_api.db import SessionLocal

    with SessionLocal() as probe:
        engine = probe.get_bind()
    sequences: list[list[str]] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        sequences[-1].append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        for email in ("bob@example.com", "nobody@example.com"):
            sequences.append([])
            assert _invite(client, team["owner"], team["ws"], email).status_code == 201
    finally:
        event.remove(engine, "before_cursor_execute", record)
    for statements in sequences:
        # Authentication reads the *inviter's* row by primary key; nothing else may.
        users = [" ".join(s.split()) for s in statements if "users" in s]
        assert len(users) == 1, users
        assert users[0].endswith("FROM users WHERE users.id = %(pk_1)s::UUID"), users
    assert sequences[0] == sequences[1]


def test_invite_timing_does_not_depend_on_registration(client, team, no_invite_limits):
    for i in range(15):
        register(client, f"known{i}@example.com")
    samples: dict[str, list[float]] = {"known": [], "unknown": []}
    for i in range(15):  # interleaved, so both groups share any drift
        for group in ("known", "unknown"):
            t0 = time.perf_counter()
            r = _invite(client, team["owner"], team["ws"], f"{group}{i}@example.com")
            samples[group].append((time.perf_counter() - t0) * 1000)
            assert r.status_code == 201
    known, unknown = (statistics.median(samples[g]) for g in ("known", "unknown"))
    assert abs(known - unknown) < max(5.0, 0.25 * min(known, unknown)), (known, unknown)


# ----------------------------------------------------------------------- consent
def test_membership_exists_only_after_the_invitee_accepts(client, team):
    token = _token(client, team["owner"], team["ws"], "bob@example.com", "editor")
    assert "bob@example.com" not in _members(client, team["owner"], team["ws"])
    assert client.get(f"/api/v1/workspaces/{team['ws']}", headers=team["bob"]).status_code == 404
    preview = client.post(PREVIEW, headers=team["bob"], json={"token": token})
    assert preview.status_code == 200
    assert preview.json()["role"] == "editor"
    assert preview.json()["invited_by"] == "owner"
    r = client.post(ACCEPT, headers=team["bob"], json={"token": token})
    assert r.status_code == 200, r.text
    assert _members(client, team["owner"], team["ws"])["bob@example.com"] == "editor"


def test_declined_invitation_adds_no_member_and_cannot_be_accepted(client, team):
    token = _token(client, team["owner"], team["ws"], "bob@example.com")
    assert client.post(DECLINE, headers=team["bob"], json={"token": token}).status_code == 204
    assert client.post(ACCEPT, headers=team["bob"], json={"token": token}).status_code == 409
    assert "bob@example.com" not in _members(client, team["owner"], team["ws"])


def test_direct_add_member_is_gone(client, team):
    r = client.post(
        f"/api/v1/workspaces/{team['ws']}/members",
        headers=team["owner"],
        json={"email": "bob@example.com", "role": "viewer"},
    )
    assert r.status_code == 405


def test_accept_link_is_shown_once(client, team):
    created = _invite(client, team["owner"], team["ws"], "bob@example.com")
    assert created.json()["accept_path"].startswith("/invite#token=")
    listed = client.get(f"/api/v1/workspaces/{team['ws']}/invitations", headers=team["owner"])
    assert "accept_path" not in listed.json()[0]
    assert accept_token(created) not in listed.text


# ------------------------------------------------------------ token lifecycle
def test_token_is_single_use(client, team):
    token = _token(client, team["owner"], team["ws"], "bob@example.com")
    assert client.post(ACCEPT, headers=team["bob"], json={"token": token}).status_code == 200
    replay = client.post(ACCEPT, headers=team["bob"], json={"token": token})
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "invitation_used"


def test_expired_token_is_rejected(client, team):
    from sqlalchemy import update

    from quantlab_api.db import SessionLocal
    from quantlab_api.invitations import Invitation

    token = _token(client, team["owner"], team["ws"], "bob@example.com")
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
    created = _invite(client, team["owner"], team["ws"], "bob@example.com")
    revoke = client.delete(
        f"/api/v1/workspaces/{team['ws']}/invitations/{created.json()['id']}",
        headers=team["owner"],
    )
    assert revoke.status_code == 204
    r = client.post(ACCEPT, headers=team["bob"], json={"token": accept_token(created)})
    assert r.status_code == 409
    assert "bob@example.com" not in _members(client, team["owner"], team["ws"])


def test_reinvite_rotates_the_token(client, team):
    old = _token(client, team["owner"], team["ws"], "bob@example.com", "viewer")
    new = _token(client, team["owner"], team["ws"], "bob@example.com", "editor")
    assert old != new
    assert client.post(ACCEPT, headers=team["bob"], json={"token": old}).status_code == 409
    assert client.post(ACCEPT, headers=team["bob"], json={"token": new}).status_code == 200
    assert _members(client, team["owner"], team["ws"])["bob@example.com"] == "editor"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t[:-1] + ("A" if t[-1] != "A" else "B"),
        lambda t: t[1:] + "x",
        lambda t: t.swapcase(),
        lambda t: "x" * 43,
    ],
)
def test_tampered_tokens_are_rejected(client, team, mutate):
    token = _token(client, team["owner"], team["ws"], "bob@example.com")
    r = client.post(ACCEPT, headers=team["bob"], json={"token": mutate(token)})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "invitation_not_found"
    assert client.post(ACCEPT, headers=team["bob"], json={"token": token}).status_code == 200


def test_token_for_another_account_reveals_nothing(client, team):
    """Registration does not verify email ownership, so the token alone is not enough:
    the signed-in account must be the invited address. The answer is the same as for a
    token that does not exist."""
    token = _token(client, team["owner"], team["ws"], "bob@example.com")
    mallory = register(client, "mallory@example.com")
    for path in (PREVIEW, ACCEPT, DECLINE):
        stolen = client.post(path, headers=mallory, json={"token": token})
        unknown = client.post(path, headers=mallory, json={"token": "y" * 43})
        assert stolen.status_code == unknown.status_code == 404, path
        strip = lambda r: {k: v for k, v in r.json()["error"].items() if k != "request_id"}  # noqa: E731
        assert strip(stolen) == strip(unknown)
    assert "mallory@example.com" not in _members(client, team["owner"], team["ws"])
    # Bob can still accept: Mallory's attempts did not consume the invitation.
    assert client.post(ACCEPT, headers=team["bob"], json={"token": token}).status_code == 200


def test_unauthenticated_invitee_endpoints_require_login(client, team):
    for path in (PREVIEW, ACCEPT, DECLINE):
        assert client.post(path, json={"token": "x" * 43}).status_code == 401


def test_email_match_is_case_insensitive(client, team):
    token = _token(client, team["owner"], team["ws"], "Bob@Example.COM")
    assert client.post(ACCEPT, headers=team["bob"], json={"token": token}).status_code == 200


def test_tokens_are_stored_only_as_hashes(client, team):
    from quantlab_api.db import SessionLocal
    from quantlab_api.invitations import Invitation, hash_token
    from quantlab_api.models import AuditEvent

    token = _token(client, team["owner"], team["ws"], "bob@example.com")
    assert len(token) >= 43  # 256 bits, base64url
    with SessionLocal() as db:
        inv = db.scalar(select(Invitation))
        assert inv is not None and inv.token_hash == hash_token(token) != token
        details = [str(a.details) for a in db.scalars(select(AuditEvent))]
    assert not any(token in d for d in details)


def test_delivery_failure_does_not_log_the_token(client, team, monkeypatch, capsys):
    """Tracebacks are logged with frame locals; the failure path must not log one."""
    from quantlab_api import invitations

    sent: list[str] = []

    class Broken:
        def send_invitation(self, message: Any) -> None:
            sent.append(message.accept_path)
            raise ConnectionError("mail relay down")

    real = invitations.notifier
    monkeypatch.setattr(invitations, "notifier", Broken())
    r = _invite(client, team["owner"], team["ws"], "bob@example.com")
    assert r.status_code == 502
    token = sent[0].split("#token=", 1)[1]
    out = capsys.readouterr()
    assert "invitation_delivery_failed" in out.out + out.err
    assert token not in out.out + out.err
    # Resending works.
    monkeypatch.setattr(invitations, "notifier", real)
    retry = _invite(client, team["owner"], team["ws"], "bob@example.com")
    assert retry.status_code == 201
    assert client.post(ACCEPT, headers=team["bob"], json={"token": accept_token(retry)}).is_success


def test_concurrent_accepts_admit_exactly_once(client, team):
    from fastapi.testclient import TestClient

    from quantlab_api.db import SessionLocal
    from quantlab_api.main import app
    from quantlab_api.models import WorkspaceMember

    token = _token(client, team["owner"], team["ws"], "bob@example.com", "editor")
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


# ------------------------------------------------------- the inviter's authority
@pytest.mark.parametrize("change", ["demote", "remove"])
def test_invitations_die_with_the_inviters_ownership(client, team, change):
    """A rogue owner's invitation must not outlive their removal: otherwise an accomplice
    could accept later as owner and take the workspace over."""
    rogue = register(client, "rogue@example.com")
    join_workspace(client, team["owner"], team["ws"], rogue, "rogue@example.com", "owner")
    accomplice = register(client, "accomplice@example.com")
    token = _token(client, rogue, team["ws"], "accomplice@example.com", "owner")
    pending = _token(client, rogue, team["ws"], "later@example.com", "owner")
    url = f"/api/v1/workspaces/{team['ws']}/members/{_uid(client, rogue)}"
    if change == "demote":
        assert client.patch(url, headers=team["owner"], json={"role": "viewer"}).status_code == 200
    else:
        assert client.delete(url, headers=team["owner"]).status_code == 204
    for path in (PREVIEW, ACCEPT):
        r = client.post(path, headers=accomplice, json={"token": token})
        assert r.status_code == 409, (path, r.text)
    assert "accomplice@example.com" not in _members(client, team["owner"], team["ws"])
    listed = client.get(f"/api/v1/workspaces/{team['ws']}/invitations", headers=team["owner"])
    assert listed.json() == []  # the rogue's other invitation was withdrawn too
    later = register(client, "later@example.com")
    assert client.post(ACCEPT, headers=later, json={"token": pending}).status_code == 409


def test_withdrawn_check_covers_invitations_from_before_the_revocation_hook(client, team):
    """Even if an inviter lost owner some other way, accept re-checks under a lock."""
    from sqlalchemy import update

    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Role, WorkspaceMember

    rogue = register(client, "rogue@example.com")
    join_workspace(client, team["owner"], team["ws"], rogue, "rogue@example.com", "owner")
    token = _token(client, rogue, team["ws"], "bob@example.com", "owner")
    with SessionLocal() as db:
        db.execute(
            update(WorkspaceMember)
            .where(WorkspaceMember.user_id == uuid.UUID(_uid(client, rogue)))
            .values(role=Role.editor)
        )
        db.commit()
    r = client.post(ACCEPT, headers=team["bob"], json={"token": token})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "invitation_withdrawn"


# ------------------------------------------------------------------ authorization
def test_authorization_matrix(client, team):
    viewer = register(client, "viewer@example.com")
    editor = register(client, "editor@example.com")
    outsider = register(client, "outsider@example.com")
    join_workspace(client, team["owner"], team["ws"], viewer, "viewer@example.com", "viewer")
    join_workspace(client, team["owner"], team["ws"], editor, "editor@example.com", "editor")
    inv = _invite(client, team["owner"], team["ws"], "x@example.com").json()
    base = f"/api/v1/workspaces/{team['ws']}/invitations"
    bob_id = _uid(client, team["bob"])
    for who, expected in ((outsider, 404), (viewer, 403), (editor, 403)):
        assert _invite(client, who, team["ws"], "y@example.com").status_code == expected
        assert client.get(base, headers=who).status_code == expected
        assert client.delete(f"{base}/{inv['id']}", headers=who).status_code == expected
        patch = client.patch(
            f"/api/v1/workspaces/{team['ws']}/members/{bob_id}", headers=who, json={"role": "owner"}
        )
        assert patch.status_code == expected
    assert client.get(base, headers=team["owner"]).status_code == 200


def test_owner_cannot_revoke_another_workspaces_invitation(client, team):
    other = register(client, "other-owner@example.com")
    other_ws = workspace_id(client, other)
    created = _invite(client, other, other_ws, "bob@example.com")
    r = client.delete(
        f"/api/v1/workspaces/{team['ws']}/invitations/{created.json()['id']}",
        headers=team["owner"],
    )
    assert r.status_code == 404
    token = accept_token(created)
    assert client.post(ACCEPT, headers=team["bob"], json={"token": token}).status_code == 200


def test_invitation_raises_but_never_lowers_a_role(client, team):
    join_workspace(client, team["owner"], team["ws"], team["bob"], "bob@example.com", "editor")
    token = _token(client, team["owner"], team["ws"], "bob@example.com", "viewer")
    client.post(ACCEPT, headers=team["bob"], json={"token": token})
    assert _members(client, team["owner"], team["ws"])["bob@example.com"] == "editor"
    token = _token(client, team["owner"], team["ws"], "bob@example.com", "owner")
    client.post(ACCEPT, headers=team["bob"], json={"token": token})
    assert _members(client, team["owner"], team["ws"])["bob@example.com"] == "owner"


def test_role_changes_keep_an_owner(client, team):
    join_workspace(client, team["owner"], team["ws"], team["bob"], "bob@example.com", "owner")
    me, bob = _uid(client, team["owner"]), _uid(client, team["bob"])
    url = f"/api/v1/workspaces/{team['ws']}/members"
    demote = client.patch(f"{url}/{me}", headers=team["owner"], json={"role": "viewer"})
    assert demote.status_code == 200
    last = client.patch(f"{url}/{bob}", headers=team["bob"], json={"role": "editor"})
    assert last.status_code == 409
    unknown = client.patch(f"{url}/{uuid.uuid4()}", headers=team["bob"], json={"role": "editor"})
    assert unknown.status_code == 404


def test_deleting_a_workspace_removes_its_invitations(client, team):
    from quantlab_api.db import SessionLocal
    from quantlab_api.invitations import Invitation

    temp = client.post("/api/v1/workspaces", headers=team["owner"], json={"name": "Temp"})
    temp_id = temp.json()["id"]
    token = _token(client, team["owner"], temp_id, "bob@example.com")
    assert client.delete(f"/api/v1/workspaces/{temp_id}", headers=team["owner"]).status_code == 204
    with SessionLocal() as db:
        left = db.scalar(select(Invitation).where(Invitation.workspace_id == uuid.UUID(temp_id)))
    assert left is None
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


def test_invitations_are_rate_limited_per_workspace(client, team, monkeypatch):
    from quantlab_api.routes import invitations as routes

    monkeypatch.setattr(routes, "INVITES_PER_HOUR_PER_WORKSPACE", 3)
    second = register(client, "second@example.com")
    join_workspace(client, team["owner"], team["ws"], second, "second@example.com", "owner")
    # join_workspace used one of the three; two owners share the rest.
    codes = [
        _invite(client, who, team["ws"], f"w{i}@example.com").status_code
        for i, who in enumerate((team["owner"], second, second))
    ]
    assert codes == [201, 201, 429]


def test_one_address_cannot_be_flooded_with_invitations(client, team):
    from quantlab_api.invitations import INVITES_PER_HOUR_PER_ADDRESS

    codes = [
        _invite(client, team["owner"], team["ws"], "bob@example.com").status_code
        for _ in range(INVITES_PER_HOUR_PER_ADDRESS + 1)
    ]
    assert codes == [201] * INVITES_PER_HOUR_PER_ADDRESS + [429]
    assert _invite(client, team["owner"], team["ws"], "carol@example.com").status_code == 201


def test_pending_invitations_are_capped_per_workspace(client, team, monkeypatch):
    from quantlab_api.routes import invitations as routes

    monkeypatch.setattr(routes, "MAX_PENDING_PER_WORKSPACE", 3)
    codes = [
        _invite(client, team["owner"], team["ws"], f"p{i}@example.com").status_code
        for i in range(4)
    ]
    assert codes == [201, 201, 201, 429]
    # Re-inviting a pending address replaces it rather than counting twice.
    assert _invite(client, team["owner"], team["ws"], "p0@example.com").status_code == 201


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


# ---------------------------------------------------------------- lock ordering
def _member_locks(statements: list[str]) -> list[str]:
    return [
        " ".join(s.split())
        for s in statements
        if "workspace_members" in s and ("FOR UPDATE" in s or "FOR SHARE" in s)
    ]


def test_member_rows_are_locked_once_in_user_id_order(client, team):
    """#37 review: accept locked the inviter's row and then the invitee's, while a role
    change locked the owners and then its target. Every path now takes its member locks
    in one statement ordered by user id, so no two can wait on each other in a cycle."""
    from quantlab_api.db import SessionLocal

    join_workspace(client, team["owner"], team["ws"], team["bob"], "bob@example.com", "editor")
    token = _token(client, team["owner"], team["ws"], "bob@example.com", "owner")
    with SessionLocal() as probe:
        engine = probe.get_bind()
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert client.post(ACCEPT, headers=team["bob"], json={"token": token}).status_code == 200
        accept = _member_locks(statements)
        statements.clear()
        url = f"/api/v1/workspaces/{team['ws']}/members/{_uid(client, team['bob'])}"
        assert client.patch(url, headers=team["owner"], json={"role": "editor"}).status_code == 200
        patch = _member_locks(statements)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    for locks in (accept, patch):
        assert len(locks) == 1, locks
        assert "ORDER BY workspace_members.user_id FOR UPDATE" in locks[0], locks


def test_accept_and_role_change_race_without_deadlock(client, no_invite_limits):
    """The reviewer's scenario: the invitee is already an owner with a lower id than the
    inviter, and accepts while another owner changes a role."""
    from tests.integration.test_concurrency import _race

    users = {n: register(client, f"{n}@example.com") for n in ("o", "p", "q", "d")}
    ids = {n: uuid.UUID(_uid(client, h)) for n, h in users.items()}
    c, x = sorted(("p", "q"), key=lambda n: ids[n])  # invitee c sorts before inviter x
    ws = workspace_id(client, users["o"])
    for name, role in ((x, "owner"), (c, "owner"), ("d", "viewer")):
        join_workspace(client, users["o"], ws, users[name], f"{name}@example.com", role)
    url = f"/api/v1/workspaces/{ws}/members/{ids['d']}"
    for trial in range(8):
        token = _token(client, users[x], ws, f"{c}@example.com", "editor")
        role = "editor" if trial % 2 else "viewer"

        def act(cl, i, token=token, role=role):  # type: ignore[no-untyped-def]
            if i == 0:
                return cl.post(ACCEPT, headers=users[c], json={"token": token}).status_code
            return cl.patch(url, headers=users["o"], json={"role": role}).status_code

        assert _race(2, act) == [200, 200], trial
