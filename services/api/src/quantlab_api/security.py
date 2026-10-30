"""Password hashing, access tokens and workspace authorization."""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
from fastapi import Depends, Request
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from quantlab_api.config import get_settings
from quantlab_api.db import get_db
from quantlab_api.errors import ApiError
from quantlab_api.models import ROLE_RANK, AuditEvent, Role, User, Workspace, WorkspaceMember

COOKIE_NAME = "quantlab_session"
_hasher = PasswordHash.recommended()  # Argon2id
# Verifying against a fixed dummy hash keeps login timing similar for unknown emails.
_DUMMY_HASH = _hasher.hash("not-a-real-password-" + uuid.uuid4().hex)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str | None) -> bool:
    if hashed is None:
        _hasher.verify(password, _DUMMY_HASH)
        return False
    try:
        return _hasher.verify(password, hashed)
    except Exception:  # malformed hash
        return False


def create_access_token(user: User) -> tuple[str, datetime]:
    s = get_settings()
    exp = datetime.now(UTC) + timedelta(minutes=s.access_token_ttl_minutes)
    payload = {
        "sub": str(user.id),
        "ver": user.token_version,
        "exp": exp,
        "iat": datetime.now(UTC),
        "iss": "quantlab",
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, s.jwt_secret.get_secret_value(), algorithm=s.jwt_algorithm)
    return token, exp


def decode_token(token: str) -> dict[str, Any]:
    s = get_settings()
    try:
        return jwt.decode(
            token,
            s.jwt_secret.get_secret_value(),
            algorithms=[s.jwt_algorithm],
            issuer="quantlab",
            options={"require": ["exp", "sub", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ApiError(401, "token_expired", "Session expired; sign in again") from exc
    except jwt.InvalidTokenError as exc:
        raise ApiError(401, "invalid_token", "Invalid credentials") from exc


def _token_from_request(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE_NAME)


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    token = _token_from_request(request)
    if not token:
        raise ApiError(401, "not_authenticated", "Authentication required")
    claims = decode_token(token)
    try:
        uid = uuid.UUID(str(claims["sub"]))
    except ValueError as exc:
        raise ApiError(401, "invalid_token", "Invalid credentials") from exc
    user = db.get(User, uid)
    # token_version lets logout-everywhere / password change revoke outstanding tokens.
    if user is None or not user.is_active or claims.get("ver") != user.token_version:
        raise ApiError(401, "invalid_token", "Invalid credentials")
    request.state.user_id = str(user.id)
    return user


CurrentUser = Annotated[User, Depends(current_user)]
DB = Annotated[Session, Depends(get_db)]


def require_workspace(
    db: Session, user: User, workspace_id: uuid.UUID, min_role: Role = Role.viewer
) -> tuple[Workspace, Role]:
    """Authorize access to a workspace. Non-members get 404 so ids cannot be probed."""
    row = db.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(Workspace.id == workspace_id, WorkspaceMember.user_id == user.id)
    ).first()
    if row is None:
        raise ApiError(404, "not_found", "Workspace not found")
    ws, role = row
    if ROLE_RANK[role] < ROLE_RANK[min_role]:
        raise ApiError(403, "forbidden", f"Requires {min_role.value} role")
    return ws, role


def audit(
    db: Session,
    request: Request | None,
    action: str,
    *,
    user: User | None = None,
    workspace_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: Any = None,
    **details: Any,
) -> None:
    db.add(
        AuditEvent(
            user_id=user.id if user else None,
            workspace_id=workspace_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            details=details,
            request_id=getattr(request.state, "request_id", None) if request else None,
            ip=request.client.host if request and request.client else None,
        )
    )


class RateLimiter:
    """Fixed-window counters shared across API replicas through Redis.

    ``reserve`` increments and compares in one atomic Redis ``INCR``, so concurrent requests
    cannot all pass a check-then-record gap. ``refund`` gives back one attempt.

    Degraded mode (Redis unavailable): counts go to a fixed table of ``SHARDS`` counters per
    process, indexed by a keyed hash with a random per-process secret. Memory is constant, lookups are O(1), and nothing is ever
    evicted, so a key flood cannot reset anyone's counter. Hash collisions can only make
    throttling stricter, never looser. Refunds are ignored in degraded mode because a shard
    may be shared by several principals. See DECISIONS.md (ADR-0007).
    """

    SHARDS = 4096

    def __init__(self) -> None:
        self._counts = [0] * self.SHARDS
        self._windows = [0] * self.SHARDS
        # Keyed hash with a per-process secret: shard collisions cannot be precomputed.
        self._salt = os.urandom(16)

    @staticmethod
    def _bucket(key: str, window: int) -> str:
        return f"ql:rl:{key}:{int(time.time() // window)}"

    def _shard(self, key: str) -> int:
        digest = hashlib.blake2b(key.encode(), key=self._salt, digest_size=8).digest()
        return int.from_bytes(digest) % self.SHARDS

    def _local_reserve(self, key: str, limit: int, window: int) -> bool:
        i, w = self._shard(key), int(time.time() // window)
        if self._windows[i] != w:
            self._windows[i], self._counts[i] = w, 0
        self._counts[i] += 1
        return self._counts[i] <= limit

    def reserve(self, key: str, limit: int, window: int = 60) -> bool:
        """Atomically count one attempt; False if it exceeds ``limit`` in this window."""
        from quantlab_api.jobs import redis_conn

        bucket = self._bucket(key, window)
        try:
            r = redis_conn()
            count = int(r.incr(bucket))
            if count == 1:
                r.expire(bucket, window + 1)
            return count <= limit
        except Exception:
            return self._local_reserve(key, limit, window)

    def refund(self, key: str, window: int = 60) -> None:
        """Give back one reserved attempt (e.g. the login succeeded). Redis only."""
        from quantlab_api.jobs import redis_conn

        try:
            redis_conn().decr(self._bucket(key, window))
        except Exception:
            return

    def count(self, key: str, window: int = 60) -> int:
        from quantlab_api.jobs import redis_conn

        try:
            return int(redis_conn().get(self._bucket(key, window)) or 0)
        except Exception:
            i = self._shard(key)
            return self._counts[i] if self._windows[i] == int(time.time() // window) else 0

    def check(self, key: str, limit: int, window: int = 60) -> None:
        if not self.reserve(key, limit, window):
            raise ApiError(429, "rate_limited", "Too many attempts; try again later")

    def reset(self) -> None:
        self._counts = [0] * self.SHARDS
        self._windows = [0] * self.SHARDS


login_limiter = RateLimiter()
