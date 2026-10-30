from __future__ import annotations

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from quantlab_api.config import get_settings
from quantlab_api.errors import ApiError
from quantlab_api.models import Role, User, Workspace, WorkspaceMember
from quantlab_api.schemas import LoginIn, RegisterIn, TokenOut, UserOut
from quantlab_api.security import (
    COOKIE_NAME,
    DB,
    CurrentUser,
    audit,
    create_access_token,
    hash_password,
    login_limiter,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue(response: Response, user: User) -> TokenOut:
    token, exp = create_access_token(user)
    s = get_settings()
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=s.cookie_secure,
        samesite="lax",
        max_age=s.access_token_ttl_minutes * 60,
        path="/",
    )
    return TokenOut(access_token=token, expires_at=exp, user=UserOut.model_validate(user))


@router.post("/register", response_model=TokenOut, status_code=201)
def register(body: RegisterIn, request: Request, response: Response, db: DB) -> TokenOut:
    ip = request.client.host if request.client else "unknown"
    login_limiter.check(f"register:{ip}", get_settings().rate_limit_login_per_minute)
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise ApiError(409, "email_taken", "An account with this email already exists")
    user = User(
        email=email,
        password_hash=hash_password(body.password),
        display_name=body.display_name.strip(),
    )
    db.add(user)
    db.flush()
    ws = Workspace(
        name=f"{user.display_name}'s workspace",
        created_by=user.id,
        description="Default personal research workspace",
    )
    db.add(ws)
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=Role.owner))
    audit(
        db,
        request,
        "user.register",
        user=user,
        workspace_id=ws.id,
        target_type="user",
        target_id=user.id,
    )
    db.commit()
    return _issue(response, user)


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, response: Response, db: DB) -> TokenOut:
    ip = request.client.host if request.client else "unknown"
    # Keyed by client and by account so distributed guessing against one account is
    # throttled even when it arrives from many addresses.
    login_limiter.check_all(
        [f"login:ip:{ip}", f"login:acct:{body.email.lower()}"],
        get_settings().rate_limit_login_per_minute,
    )
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if (
        not verify_password(body.password, user.password_hash if user else None)
        or not user
        or not user.is_active
    ):
        audit(
            db,
            request,
            "user.login_failed",
            target_type="email",
            target_id=None,
            email_domain=body.email.split("@")[-1],
        )
        db.commit()
        raise ApiError(401, "invalid_credentials", "Invalid email or password")
    audit(db, request, "user.login", user=user, target_type="user", target_id=user.id)
    db.commit()
    return _issue(response, user)


@router.post("/logout", status_code=204)
def logout(
    response: Response, user: CurrentUser, request: Request, db: DB, everywhere: bool = False
) -> Response:
    if everywhere:
        user.token_version += 1
        audit(db, request, "user.logout_everywhere", user=user)
        db.commit()
    response.delete_cookie(COOKIE_NAME, path="/")
    response.status_code = 204
    return response


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> User:
    return user
