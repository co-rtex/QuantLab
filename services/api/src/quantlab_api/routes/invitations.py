"""Invitation endpoints (#5): owners invite and revoke; invitees preview, accept or decline.

``owner_router`` is mounted under ``/workspaces`` and ``invitee_router`` under ``/auth``
(as ``/auth/invitations/...``), since an invitee acts for the signed-in account.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from quantlab_api import invitations as inv_mod
from quantlab_api.errors import ApiError
from quantlab_api.invitations import (
    INVITES_PER_HOUR_PER_ADDRESS,
    INVITES_PER_HOUR_PER_INVITER,
    INVITES_PER_HOUR_PER_WORKSPACE,
    MAX_PENDING_PER_WORKSPACE,
    Invitation,
    InvitationCreated,
    InvitationIn,
    InvitationMessage,
    InvitationOut,
    InvitationPreview,
    InvitationTokenIn,
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


@owner_router.post("/{workspace_id}/invitations", response_model=InvitationCreated, status_code=201)
def create_invitation(
    workspace_id: uuid.UUID, body: InvitationIn, user: CurrentUser, db: DB, request: Request
) -> InvitationCreated:
    """Invite an address and return its accept link, once, for the owner to pass on.

    The response is the same whether or not the address has an account: this path never
    reads the users table, and its limits do not depend on the address having one.
    """
    ws, _ = require_workspace(db, user, workspace_id, Role.owner)
    email = body.email.lower()
    login_limiter.check(f"invite:user:{user.id}", INVITES_PER_HOUR_PER_INVITER, 3600)
    login_limiter.check(f"invite:ws:{workspace_id}", INVITES_PER_HOUR_PER_WORKSPACE, 3600)
    login_limiter.check(f"invite:to:{workspace_id}:{email}", INVITES_PER_HOUR_PER_ADDRESS, 3600)
    now = datetime.now(UTC)
    # Serializes invitation writes per workspace: the pending cap and the re-invite
    # revocation below would otherwise race. NO KEY UPDATE, so inserts that reference the
    # workspace (members, audit events) are not blocked and cannot deadlock with it.
    db.execute(
        select(Workspace.id).where(Workspace.id == workspace_id).with_for_update(key_share=True)
    )
    # The inviter may have been demoted while this request waited for the lock.
    inviter = lock_members(db, workspace_id, {user.id}).get(user.id)
    if inviter is None or inviter.role != Role.owner:
        raise ApiError(403, "forbidden", "Requires owner role")
    # Expired invitations can never be answered; drop them rather than keep them forever.
    db.execute(
        delete(Invitation).where(
            Invitation.workspace_id == workspace_id,
            Invitation.status == "pending",
            Invitation.expires_at <= now,
        )
    )
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
    accept_path = f"/invite#token={token}"
    message = InvitationMessage(
        to=email,
        workspace_name=ws.name,
        inviter_name=user.display_name,
        role=body.role,
        accept_path=accept_path,
        expires_at=invitation.expires_at.isoformat(),
    )
    try:
        inv_mod.notifier.send_invitation(message)
    except Exception as exc:
        # No traceback: its frame locals would put the live token in the log.
        log.error(
            "invitation_delivery_failed",
            invitation_id=str(invitation.id),
            error=type(exc).__name__,
        )
        raise ApiError(
            502, "delivery_failed", "The invitation was saved but could not be sent; resend it"
        ) from None
    return InvitationCreated(**_out(invitation).model_dump(), accept_path=accept_path)


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


def lock_members(
    db: Session, workspace_id: uuid.UUID, user_ids: set[uuid.UUID], *, owners: bool = False
) -> dict[uuid.UUID, WorkspaceMember]:
    """Lock these members' rows (and every owner's, with ``owners``) in one statement, in
    user-id order, and return them freshly read.

    Every path that locks member rows goes through here (accept, preview, role change,
    removal), so they all lock in one global order and cannot deadlock. Accept used to lock
    the inviter's row and then the invitee's while a role change locked the owners and then
    its target, which deadlocked when an invitee with a lower id was already an owner.
    """
    match = WorkspaceMember.user_id.in_(user_ids)
    rows = db.scalars(
        select(WorkspaceMember)
        .where(
            WorkspaceMember.workspace_id == workspace_id,
            or_(match, WorkspaceMember.role == Role.owner) if owners else match,
        )
        .order_by(WorkspaceMember.user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    return {m.user_id: m for m in rows}


def _require_inviter_still_owner(
    db: Session, i: Invitation, locked: dict[uuid.UUID, WorkspaceMember]
) -> None:
    """An invitation carries its inviter's authority: once they are no longer an owner (or
    no longer a member) it is withdrawn. ``locked`` holds the inviter's row, locked, so a
    concurrent demotion or removal finishes first and is seen."""
    inviter = locked.get(i.invited_by) if i.invited_by is not None else None
    if inviter is None or inviter.role != Role.owner:
        db.execute(
            update(Invitation)
            .where(Invitation.id == i.id, Invitation.status == "pending")
            .values(status="revoked", responded_at=datetime.now(UTC))
        )
        db.commit()
        raise ApiError(409, "invitation_withdrawn", "This invitation was withdrawn")


def revoke_invitations_from(db: Session, workspace_id: uuid.UUID, inviter: uuid.UUID) -> None:
    """Withdraw the pending invitations a member sent, when they stop being an owner."""
    db.execute(
        update(Invitation)
        .where(
            Invitation.workspace_id == workspace_id,
            Invitation.invited_by == inviter,
            Invitation.status == "pending",
        )
        .values(status="revoked", responded_at=datetime.now(UTC))
    )


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
    inviter_ids = {i.invited_by} if i.invited_by is not None else set()
    _require_inviter_still_owner(db, i, lock_members(db, i.workspace_id, inviter_ids))
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
    wanted = {user.id} | ({i.invited_by} if i.invited_by is not None else set())
    locked = lock_members(db, i.workspace_id, wanted)
    _require_inviter_still_owner(db, i, locked)
    _respond(db, i, user, "accepted")
    role = Role(i.role)
    member = locked.get(user.id)
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
