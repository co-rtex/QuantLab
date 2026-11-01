from __future__ import annotations

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from quantlab_api.config import get_settings
from quantlab_api.errors import ApiError
from quantlab_api.models import Role, User, Workspace, WorkspaceMember
from quantlab_api.observability import log
from quantlab_api.routes.invitations import invitee_router as invitee_routes
from quantlab_api.schemas import LoginIn, RegisterIn, TokenOut, UserOut
from quantlab_api.security import (
    COOKIE_NAME,
    DB,
    DEVICE_FAILURES,
    DEVICE_WINDOW,
    CurrentUser,
    Reservation,
    audit,
    create_access_token,
    hash_password,
    login_limiter,
    rate_limit_address,
    rate_limited,
    set_device_cookie,
    trusted_device,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _throttle_address(request: Request) -> str | None:
    """The address to throttle on, or None (no TLS edge in development: ADR-0008).

    In production an unknown address means the edge is missing or misconfigured, so the
    request is refused rather than served without a per-address limit.
    """
    ip = rate_limit_address(request)
    if ip is None and get_settings().environment == "production":
        log.error("edge_missing", path=request.url.path)
        raise ApiError(503, "edge_required", "The service is misconfigured; try again later")
    return ip


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
    limit = get_settings().rate_limit_login_per_minute
    ip = _throttle_address(request)
    if ip is not None:
        login_limiter.check(f"register:{ip}", limit)
    else:
        # Development without an edge: a coarse cap on everyone's registrations together.
        # It can delay sign-ups but never locks existing users out of their accounts.
        login_limiter.check("register:unknown", limit * 10)
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
    ip = _throttle_address(request)
    limit = get_settings().rate_limit_login_per_minute
    # Every attempt counts against the client address. Only *failed* attempts count against
    # the account, with a higher threshold, so distributed guessing is still throttled.
    # A client that reaches the API from many addresses can still spend an account's
    # budget (ADR-0008); what the edge guarantees is that it cannot choose those addresses.
    # Without a known address (development without a TLS edge) only the account limit
    # applies: one shared bucket for everyone would let ten failed logins lock every user
    # out. Production refuses such requests instead (_throttle_address).
    if ip is not None:
        login_limiter.check(f"login:ip:{ip}", limit)
    email = body.email.lower()
    user = db.scalar(select(User).where(User.email == email))
    # A browser that signed in to this account before presents a device cookie: it is
    # throttled per device, so an attacker who spends the account's budget from a few
    # addresses cannot lock the owner out of it (#55, ADR-0008). Its own failures are
    # capped too, and past that cap it falls back to the account budget. Reserve
    # atomically before verifying; refunded below if the login succeeds.
    device = trusted_device(request, email, user)
    budget: Reservation | None = None
    if device is not None:
        dev = login_limiter.reserve_receipt(f"login:dev:{device}", DEVICE_FAILURES, DEVICE_WINDOW)
        if dev:
            budget = dev
    if budget is None:
        budget = login_limiter.reserve_receipt(f"login:fail:{email}", limit * 3)
        if not budget:
            raise rate_limited(60, "Too many failed sign-in attempts for this account")
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
    login_limiter.refund(budget)  # where it was counted: Redis, or nowhere
    audit(db, request, "user.login", user=user, target_type="user", target_id=user.id)
    db.commit()
    set_device_cookie(response, user, device)
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


router.include_router(invitee_routes)
