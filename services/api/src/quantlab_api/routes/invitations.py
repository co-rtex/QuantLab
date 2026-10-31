"""Invitation endpoints (#5): owners invite and revoke; invitees preview, accept or decline.

``owner_router`` is mounted under ``/workspaces`` and ``invitee_router`` under ``/auth``
(as ``/auth/invitations/...``), since an invitee acts for the signed-in account.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from quantlab_api import invitations as inv_mod
from quantlab_api.config import get_settings
from quantlab_api.errors import ApiError
from quantlab_api.invitations import (
    INVITES_PER_HOUR_PER_INVITER,
    INVITES_PER_HOUR_PER_WORKSPACE,
    MAX_PENDING_PER_WORKSPACE,
    Invitation,
    InvitationIn,
    InvitationMessage,
    InvitationOut,
    InvitationPreview,
    InvitationTokenIn,
    MailboxItem,
    OutboxNotifier,
    expiry,
    hash_token,
    new_token,
)
from quantlab_api.models import ROLE_RANK, Role, User, Workspace, WorkspaceMember
from quantlab_api.observability import log
from quantlab_api.schemas import MemberOut
from quantlab_api.security import DB, CurrentUser, audit, login_limiter, require_workspace

owner_router = APIRouter(tags=["invitations"])
invitee_router = APIRouter(prefix="/invitations", tags=["invitations"])

NOT_FOUND = "This invitation is invalid, has expired, or is addressed to another account"


def _out(i: Invitation) -> InvitationOut:
    status = i.status
    if status == "pending" and i.expires_at <= datetime.now(UTC):
        status = "expired"
    return InvitationOut(
        id=i.id,
        email=i.email,
        role=i.role,
        status=status,
        created_at=i.created_at,
        expires_at=i.expires_at,
    )


@owner_router.post("/{workspace_id}/invitations", response_model=InvitationOut, status_code=201)
def create_invitation(
    workspace_id: uuid.UUID, body: InvitationIn, user: CurrentUser, db: DB, request: Request
) -> InvitationOut:
    """Invite an address. The response is the same whether or not it has an account: this
    path never reads the users table."""
    ws, _ = require_workspace(db, user, workspace_id, Role.owner)
    login_limiter.check(f"invite:user:{user.id}", INVITES_PER_HOUR_PER_INVITER, 3600)
    login_limiter.check(f"invite:ws:{workspace_id}", INVITES_PER_HOUR_PER_WORKSPACE, 3600)
    email = body.email.lower()
    now = datetime.now(UTC)
    # Serializes invitation writes per workspace: the pending cap and the re-invite
    # revocation below would otherwise race.
    db.execute(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())
    db.execute(
        update(Invitation)
        .where(
            Invitation.workspace_id == workspace_id,
            Invitation.email == email,
            Invitation.status == "pending",
        )
        .values(status="revoked", responded_at=now, responded_by=user.id)
    )
    pending = db.scalar(
        select(func.count())
        .select_from(Invitation)
        .where(
            Invitation.workspace_id == workspace_id,
            Invitation.status == "pending",
            Invitation.expires_at > now,
        )
    )
    if (pending or 0) >= MAX_PENDING_PER_WORKSPACE:
        raise ApiError(429, "too_many_invitations", "Revoke unused invitations first")
    token, token_hash = new_token()
    invitation = Invitation(
        workspace_id=workspace_id,
        email=email,
        role=body.role,
        token_hash=token_hash,
        invited_by=user.id,
        expires_at=expiry(),
    )
    db.add(invitation)
    db.flush()
    audit(
        db,
        request,
        "workspace.invitation_create",
        user=user,
        workspace_id=workspace_id,
        target_type="invitation",
        target_id=invitation.id,
        role=body.role,
    )
    db.commit()
    message = InvitationMessage(
        to=email,
        workspace_name=ws.name,
        inviter_name=user.display_name,
        role=body.role,
        accept_path=f"/invite#token={token}",
        expires_at=invitation.expires_at.isoformat(),
        sent_at=now.isoformat(),
    )
    try:
        inv_mod.notifier.send_invitation(message)
    except Exception as exc:
        log.exception("invitation_delivery_failed", invitation_id=str(invitation.id))
        raise ApiError(
            502, "delivery_failed", "The invitation was saved but could not be sent; resend it"
        ) from exc
    return _out(invitation)


@owner_router.get("/{workspace_id}/invitations", response_model=list[InvitationOut])
def list_invitations(workspace_id: uuid.UUID, user: CurrentUser, db: DB) -> list[InvitationOut]:
    require_workspace(db, user, workspace_id, Role.owner)
    rows = db.scalars(
        select(Invitation)
        .where(Invitation.workspace_id == workspace_id, Invitation.status == "pending")
        .order_by(Invitation.created_at.desc())
    )
    return [_out(i) for i in rows]


@owner_router.delete("/{workspace_id}/invitations/{invitation_id}", status_code=204)
def revoke_invitation(
    workspace_id: uuid.UUID,
    invitation_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    request: Request,
) -> Response:
    require_workspace(db, user, workspace_id, Role.owner)
    done = db.execute(
        update(Invitation)
        .where(
            Invitation.id == invitation_id,
            Invitation.workspace_id == workspace_id,
            Invitation.status == "pending",
        )
        .values(status="revoked", responded_at=datetime.now(UTC), responded_by=user.id)
    ).rowcount  # type: ignore[attr-defined]
    if not done:
        raise ApiError(404, "not_found", "No pending invitation with that id")
    audit(
        db,
        request,
        "workspace.invitation_revoke",
        user=user,
        workspace_id=workspace_id,
        target_type="invitation",
        target_id=invitation_id,
    )
    db.commit()
    return Response(status_code=204)


def _addressed_to(db: Session, user: User, token: str) -> Invitation:
    """The invitation for this token if it is addressed to the signed-in account.

    Any other case (unknown token, another account's invitation) is one uniform 404, so a
    token in the wrong hands reveals nothing about the workspace.
    """
    invitation = db.scalar(select(Invitation).where(Invitation.token_hash == hash_token(token)))
    if invitation is None or invitation.email != user.email.lower():
        raise ApiError(404, "invitation_not_found", NOT_FOUND)
    return invitation


def _require_pending(i: Invitation) -> None:
    if i.status != "pending":
        raise ApiError(409, "invitation_used", f"This invitation was already {i.status}")
    if i.expires_at <= datetime.now(UTC):
        raise ApiError(410, "invitation_expired", "This invitation has expired")


def _respond(db: Session, i: Invitation, user: User, status: str) -> None:
    """Move a pending, unexpired invitation to ``status`` exactly once."""
    now = datetime.now(UTC)
    done = db.execute(
        update(Invitation)
        .where(
            Invitation.id == i.id,
            Invitation.status == "pending",
            Invitation.expires_at > now,
        )
        .values(status=status, responded_at=now, responded_by=user.id)
    ).rowcount  # type: ignore[attr-defined]
    if not done:
        db.rollback()
        db.refresh(i)
        _require_pending(i)
        raise ApiError(409, "invitation_used", "This invitation was already answered")


@invitee_router.post("/preview", response_model=InvitationPreview)
def preview_invitation(body: InvitationTokenIn, user: CurrentUser, db: DB) -> InvitationPreview:
    i = _addressed_to(db, user, body.token)
    _require_pending(i)
    ws = db.get(Workspace, i.workspace_id)
    inviter = db.get(User, i.invited_by) if i.invited_by else None
    assert ws is not None
    return InvitationPreview(
        workspace_id=ws.id,
        workspace_name=ws.name,
        role=i.role,
        invited_by=inviter.display_name if inviter else None,
        expires_at=i.expires_at,
    )


@invitee_router.post("/accept", response_model=MemberOut)
def accept_invitation(
    body: InvitationTokenIn, user: CurrentUser, db: DB, request: Request
) -> MemberOut:
    i = _addressed_to(db, user, body.token)
    _require_pending(i)
    _respond(db, i, user, "accepted")
    role = Role(i.role)
    member = db.get(WorkspaceMember, (i.workspace_id, user.id), with_for_update=True)
    if member is None:
        member = WorkspaceMember(workspace_id=i.workspace_id, user_id=user.id, role=role)
        db.add(member)
    elif ROLE_RANK[role] > ROLE_RANK[member.role]:
        member.role = role  # an invitation can raise a role, never lower it
    audit(
        db,
        request,
        "workspace.invitation_accept",
        user=user,
        workspace_id=i.workspace_id,
        target_type="invitation",
        target_id=i.id,
        role=member.role.value,
    )
    db.commit()
    return MemberOut(
        user_id=user.id, email=user.email, display_name=user.display_name, role=member.role.value
    )


@invitee_router.post("/decline", status_code=204)
def decline_invitation(
    body: InvitationTokenIn, user: CurrentUser, db: DB, request: Request
) -> Response:
    i = _addressed_to(db, user, body.token)
    _require_pending(i)
    _respond(db, i, user, "declined")
    audit(
        db,
        request,
        "workspace.invitation_decline",
        user=user,
        workspace_id=i.workspace_id,
        target_type="invitation",
        target_id=i.id,
    )
    db.commit()
    return Response(status_code=204)


@invitee_router.get("/dev-mailbox", response_model=list[MailboxItem])
def dev_mailbox(user: CurrentUser) -> list[MailboxItem]:
    """Development and test only: the local outbox messages sent to your own address.

    Stands in for an email inbox so invitations can be accepted without a mail service.
    Disabled in production, where a ``Notifier`` adapter delivers messages instead.
    """
    n = inv_mod.notifier
    if get_settings().environment == "production" or not isinstance(n, OutboxNotifier):
        raise ApiError(404, "not_found", "Not found")
    return [
        MailboxItem(
            to=m["to"],
            subject=f"{m['inviter_name']} invited you to {m['workspace_name']} as {m['role']}",
            accept_path=m["accept_path"],
            sent_at=datetime.fromisoformat(m["sent_at"]),
        )
        for m in n.messages_for(user.email.lower())
    ]
