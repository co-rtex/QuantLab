"""Password hashing, access tokens and workspace authorization."""

from __future__ import annotations

import hashlib
import time
import uuid
from collections import OrderedDict, deque
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
    cannot all pass a check-then-record gap.

    If Redis is unavailable, a bounded per-process fallback keeps credential endpoints
    protected. It is an LRU keyed by last touch: expired entries are dropped from the front
    in amortized O(1); when full, the least-recently-used counter that is *not* currently
    throttled is evicted, so throttled (security-relevant) counters survive key floods. Only
    if every tracked counter is throttled do new keys fall into one of ``OVERFLOW_SHARDS``
    hashed overflow counters, which bounds memory without one global lockout.
    """

    MAX_LOCAL_KEYS = 10_000
    OVERFLOW_SHARDS = 64

    def __init__(self) -> None:
        # key -> (timestamps, limit); ordered by last touch (oldest first)
        self.local: OrderedDict[str, tuple[deque[float], int]] = OrderedDict()
        self.alias: dict[str, str] = {}

    @staticmethod
    def _bucket(key: str, window: int) -> str:
        return f"ql:rl:{key}:{int(time.time() // window)}"

    def _overflow_key(self, key: str) -> str:
        shard = int(hashlib.sha256(key.encode()).hexdigest(), 16) % self.OVERFLOW_SHARDS
        return f"__overflow__:{shard}"

    def _local_entry(self, key: str, limit: int, window: float) -> deque[float]:
        now = time.monotonic()
        key = self.alias.get(key, key)
        while self.local:
            oldest_key, (oldest, _) = next(iter(self.local.items()))
            if oldest and now - oldest[-1] <= window:
                break
            del self.local[oldest_key]
        entry = self.local.get(key)
        if entry is None and len(self.local) >= self.MAX_LOCAL_KEYS:
            victim = next((k for k, (q, lim) in self.local.items() if len(q) < lim), None)
            if victim is not None:
                del self.local[victim]
            else:
                original, key = key, self._overflow_key(key)
                self.alias[original] = key
                if len(self.alias) > self.MAX_LOCAL_KEYS:
                    self.alias.clear()
                entry = self.local.get(key)
        if entry is None:
            entry = (deque(), limit)
        q = entry[0]
        while q and now - q[0] > window:
            q.popleft()
        self.local[key] = (q, limit)
        self.local.move_to_end(key)
        return q

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
            q = self._local_entry(key, limit, window)
            q.append(time.monotonic())
            return len(q) <= limit

    def refund(self, key: str, window: int = 60) -> None:
        """Give back one reserved attempt (e.g. the login succeeded)."""
        from quantlab_api.jobs import redis_conn

        try:
            redis_conn().decr(self._bucket(key, window))
        except Exception:
            entry = self.local.get(self.alias.get(key, key))
            if entry and entry[0]:
                entry[0].pop()

    def count(self, key: str, window: int = 60) -> int:
        from quantlab_api.jobs import redis_conn

        try:
            return int(redis_conn().get(self._bucket(key, window)) or 0)
        except Exception:
            entry = self.local.get(self.alias.get(key, key))
            return len(entry[0]) if entry else 0

    def check(self, key: str, limit: int, window: int = 60) -> None:
        if not self.reserve(key, limit, window):
            raise ApiError(429, "rate_limited", "Too many attempts; try again later")

    def reset(self) -> None:
        self.local.clear()
        self.alias.clear()


login_limiter = RateLimiter()
