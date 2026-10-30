from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from quantlab_api.errors import ApiError
from quantlab_api.models import AuditEvent, Role, User, Workspace, WorkspaceMember
from quantlab_api.schemas import AuditOut, MemberIn, MemberOut, Page, WorkspaceIn, WorkspaceOut
from quantlab_api.security import DB, CurrentUser, audit, require_workspace

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.get("", response_model=list[WorkspaceOut])
def list_workspaces(user: CurrentUser, db: DB) -> list[WorkspaceOut]:
    rows = db.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id)
        .order_by(Workspace.created_at)
    ).all()
    return [WorkspaceOut.model_validate(w).model_copy(update={"role": r.value}) for w, r in rows]


@router.post("", response_model=WorkspaceOut, status_code=201)
def create_workspace(
    body: WorkspaceIn, user: CurrentUser, db: DB, request: Request
) -> WorkspaceOut:
    ws = Workspace(name=body.name, description=body.description, created_by=user.id)
    db.add(ws)
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=Role.owner))
    audit(
        db,
        request,
        "workspace.create",
        user=user,
        workspace_id=ws.id,
        target_type="workspace",
        target_id=ws.id,
    )
    db.commit()
    return WorkspaceOut.model_validate(ws).model_copy(update={"role": "owner"})


@router.get("/{workspace_id}", response_model=WorkspaceOut)
def get_workspace(workspace_id: uuid.UUID, user: CurrentUser, db: DB) -> WorkspaceOut:
    ws, role = require_workspace(db, user, workspace_id)
    return WorkspaceOut.model_validate(ws).model_copy(update={"role": role.value})


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
def update_workspace(
    workspace_id: uuid.UUID, body: WorkspaceIn, user: CurrentUser, db: DB, request: Request
) -> WorkspaceOut:
    ws, role = require_workspace(db, user, workspace_id, Role.editor)
    ws.name, ws.description = body.name, body.description
    audit(db, request, "workspace.update", user=user, workspace_id=ws.id)
    db.commit()
    return WorkspaceOut.model_validate(ws).model_copy(update={"role": role.value})


@router.delete("/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: uuid.UUID, user: CurrentUser, db: DB, request: Request) -> None:
    ws, _ = require_workspace(db, user, workspace_id, Role.owner)
    audit(
        db,
        request,
        "workspace.delete",
        user=user,
        target_type="workspace",
        target_id=ws.id,
        name=ws.name,
    )
    db.delete(ws)
    db.commit()


@router.get("/{workspace_id}/members", response_model=list[MemberOut])
def list_members(workspace_id: uuid.UUID, user: CurrentUser, db: DB) -> list[MemberOut]:
    require_workspace(db, user, workspace_id)
    rows = db.execute(
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == workspace_id)
    ).all()
    return [
        MemberOut(user_id=u.id, email=u.email, display_name=u.display_name, role=m.role.value)
        for m, u in rows
    ]


@router.post("/{workspace_id}/members", response_model=MemberOut, status_code=201)
def add_member(
    workspace_id: uuid.UUID, body: MemberIn, user: CurrentUser, db: DB, request: Request
) -> MemberOut:
    require_workspace(db, user, workspace_id, Role.owner)
    target = db.scalar(select(User).where(User.email == body.email.lower()))
    if target is None:
        raise ApiError(404, "user_not_found", "No user with that email")
    m = db.get(WorkspaceMember, (workspace_id, target.id))
    if m is None:
        m = WorkspaceMember(workspace_id=workspace_id, user_id=target.id, role=Role(body.role))
        db.add(m)
    else:
        m.role = Role(body.role)
    audit(
        db,
        request,
        "workspace.member_set",
        user=user,
        workspace_id=workspace_id,
        target_type="user",
        target_id=target.id,
        role=body.role,
    )
    db.commit()
    return MemberOut(
        user_id=target.id, email=target.email, display_name=target.display_name, role=m.role.value
    )


@router.delete("/{workspace_id}/members/{user_id}", status_code=204)
def remove_member(
    workspace_id: uuid.UUID, user_id: uuid.UUID, user: CurrentUser, db: DB, request: Request
) -> None:
    require_workspace(db, user, workspace_id, Role.owner)
    m = db.get(WorkspaceMember, (workspace_id, user_id))
    if m is None:
        raise ApiError(404, "not_found", "Member not found")
    owners = db.scalar(
        select(func.count())
        .select_from(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.role == Role.owner)
    )
    if m.role == Role.owner and (owners or 0) <= 1:
        raise ApiError(409, "last_owner", "Cannot remove the last owner")
    db.delete(m)
    audit(
        db,
        request,
        "workspace.member_remove",
        user=user,
        workspace_id=workspace_id,
        target_type="user",
        target_id=user_id,
    )
    db.commit()


@router.get("/{workspace_id}/audit", response_model=Page[AuditOut])
def audit_log(
    workspace_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[AuditOut]:
    require_workspace(db, user, workspace_id, Role.owner)
    q = select(AuditEvent).where(AuditEvent.workspace_id == workspace_id)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(AuditEvent.created_at.desc()).limit(limit).offset(offset)).all()
    return Page(
        items=[AuditOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )
