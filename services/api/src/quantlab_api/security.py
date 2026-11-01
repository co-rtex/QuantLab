"""Password hashing, access tokens and workspace authorization."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import threading
import time
import uuid
from array import array
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
from fastapi import Depends, Request, Response
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
    token = jwt.encode(payload, s.signing_key, algorithm=s.jwt_algorithm)
    return token, exp


def decode_token(token: str) -> dict[str, Any]:
    s = get_settings()
    try:
        return jwt.decode(
            token,
            s.signing_key,
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
    # For long requests that re-check the session after reading their body.
    request.state.token_version = user.token_version
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


Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _in_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class TrustedProxies:
    """``trusted_proxies`` (IPs, CIDRs, hostnames) as networks, without blocking requests.

    IPs and CIDRs are parsed inline. Hostnames (compose service names, whose addresses
    change when a container is recreated) are resolved by a background thread, because
    ``client_ip`` is also reached from async routes, where a slow ``getaddrinfo`` stalled
    the event loop for every request. Only a cold start in a worker thread (sync routes
    such as login) resolves inline, so the first logins are attributed correctly; one
    thread at a time, at most once per ``RETRY``, so a dead resolver cannot tie up the
    thread pool.

    Fail-safe rules, since losing the list puts every user behind the proxy into one
    rate-limit bucket: a failed lookup keeps that name's last good addresses and is retried
    after ``RETRY`` seconds, not ``REFRESH``; and a request from a peer outside every
    network wakes the resolver early (at most once per ``RETRY``; loopback peers such as
    health checks do not), so a recreated proxy is trusted again within about a second.
    """

    REFRESH = 30.0
    RETRY = 1.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._entries: tuple[str, ...] | None = None
        self._static: list[Network] = []
        self._names: tuple[str, ...] = ()
        self._resolved: dict[str, list[Network]] = {}
        self._last_wake = 0.0
        self._last_inline = 0.0
        self._inline = threading.Lock()
        self._stopped = threading.Event()
        self._owner: tuple[int, threading.Thread] | None = None

    def networks(self) -> list[Network]:
        entries = tuple(get_settings().trusted_proxies)
        with self._lock:
            if entries != self._entries:
                self._configure(entries)
            cold = bool(self._names) and not self._resolved
            if self._names:
                self._ensure_thread()
        if cold and not _in_event_loop() and self._inline.acquire(blocking=False):
            try:
                if time.monotonic() - self._last_inline >= self.RETRY:
                    self._last_inline = time.monotonic()
                    self.resolve_now()
            finally:
                self._inline.release()
        with self._lock:
            return self._static + [n for nets in self._resolved.values() for n in nets]

    def missed(self, peer: str) -> None:
        """A peer was not trusted: re-resolve soon in case a proxy moved."""
        try:
            if ipaddress.ip_address(peer).is_loopback:
                return  # health checks and local tools, never a proxy that moved
        except ValueError:
            return
        now = time.monotonic()
        if self._names and now - self._last_wake >= self.RETRY:
            self._last_wake = now
            self._wake.set()

    def _configure(self, entries: tuple[str, ...]) -> None:
        static: list[Network] = []
        names: list[str] = []
        for entry in entries:
            try:
                static.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                names.append(entry)
        self._entries, self._static, self._names = entries, static, tuple(names)
        self._resolved = {n: v for n, v in self._resolved.items() if n in names}
        self._wake.set()

    def _ensure_thread(self) -> None:
        # A forked worker inherits no threads, so the resolver is per process.
        pid = os.getpid()
        if self._owner is None or self._owner[0] != pid or not self._owner[1].is_alive():
            t = threading.Thread(target=self._run, name="trusted-proxy-resolver", daemon=True)
            self._owner = (pid, t)
            t.start()

    def resolve_now(self) -> bool:
        """Resolve every hostname once; True when all of them resolved."""
        with self._lock:
            names = self._names
        ok = True
        for name in names:
            try:
                infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
                nets = [ipaddress.ip_network(info[4][0]) for info in infos]
            except (OSError, ValueError):
                ok = False
                continue  # keep the last good addresses
            with self._lock:
                if name in self._names:
                    self._resolved[name] = nets
        return ok

    def stop(self) -> None:
        """End the resolver thread (tests)."""
        self._stopped.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stopped.is_set():
            self._wake.clear()
            ok = self.resolve_now()
            self._wake.wait(self.REFRESH if ok else self.RETRY)


trusted_proxies = TrustedProxies()


def _in(nets: list[Network], host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in n for n in nets)


def _forwarded_client(request: Request) -> tuple[str, str | None, bool]:
    """(socket peer, client address or None, whether the peer is a trusted proxy).

    Only a configured proxy may state the client address. Its ``X-Forwarded-For`` is read
    from the right, skipping further trusted proxies, so entries a client prepended itself
    are never used. One lookup of the trusted networks answers everything, so a resolver
    update in between cannot make the answers disagree.
    """
    peer = request.client.host if request.client else "unknown"
    nets = trusted_proxies.networks()
    if not _in(nets, peer):
        trusted_proxies.missed(peer)
        return peer, peer, False
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",")]
    for hop in reversed([h for h in hops if h]):
        if not _in(nets, hop):
            try:
                return peer, str(ipaddress.ip_address(hop)), True
            except ValueError:
                return peer, None, True
    return peer, None, True


def _bucket(address: str) -> str:
    """One rate-limit bucket per IPv4 address or IPv6 /64.

    An IPv6 host is normally given a whole /64, so per-/128 buckets would give one client
    about 2**64 of them. IPv4-mapped IPv6 addresses count as the IPv4 address.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((ip, 64), strict=False))
    return str(ip)


def client_ip(request: Request) -> str:
    """The client address for audit records: the stated address, else the socket peer."""
    peer, client, _ = _forwarded_client(request)
    return client or peer


def rate_limit_address(request: Request) -> str | None:
    """The address to throttle on, or None when it is unknown.

    None means the request came from a trusted proxy that stated no usable client address
    (the web tier without a TLS edge never forwards one; ADR-0008). Throttling on the
    proxy's own address would put every user into one bucket, so that ten failed logins
    anywhere lock everyone out; callers skip the per-address limit instead and rely on
    the per-account limit. IPv6 addresses are returned as their /64 (``_bucket``).

    In production a peer that is not a trusted proxy is also "unknown" (#39): clients only
    reach the API through the web tier there, so an untrusted peer means the web tier fell
    out of the trusted set (stale DNS after a recreate, a failed lookup, a wrong
    ``QUANTLAB_TRUSTED_PROXIES``), and its address would again be everyone's bucket.
    Callers then fail closed (``routes/auth.py``).
    """
    _, address, via_proxy = _forwarded_client(request)
    if address is None:
        return None
    if get_settings().environment == "production" and not via_proxy:
        return None
    return _bucket(address)


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
            ip=client_ip(request) if request else None,
        )
    )


@dataclass(frozen=True)
class Reservation:
    """One reserved attempt: whether it was allowed, its bucket, and whether Redis counted
    it (``refund`` gives it back only there)."""

    allowed: bool
    bucket: str
    window: int
    in_redis: bool

    def __bool__(self) -> bool:
        return self.allowed


class RateLimiter:
    """Fixed-window counters shared across API replicas through Redis.

    ``reserve`` increments and compares in one atomic Redis ``INCR``, so concurrent requests
    cannot all pass a check-then-record gap. ``refund`` gives back one attempt.

    Degraded mode (Redis unavailable): counts go to a fixed table of ``SHARDS`` counters
    per process and per window length, indexed by a keyed hash with a random per-process
    secret. Memory is constant per window length, lookups are O(1) and nothing is ever
    evicted, so a key flood cannot reset anyone's counter. Hash collisions can only make
    throttling stricter, never looser. That holds because each table serves one window
    length: when one-hour invitation limits shared shards with one-minute login limits,
    the two keys kept resetting each other's count (#37 review). Refunds are ignored in
    degraded mode because a shard may be shared by several principals. See DECISIONS.md
    (ADR-0007).

    Breaker (#207): after Redis fails to answer (a connection error or a timeout), degraded
    mode is used for ``BREAKER_SECONDS`` without asking Redis. A hung Redis cost every
    call a full reply timeout before the fallback, serially: three per login, 15.21 s
    live. Then one call tries Redis again while the others still count locally, and a
    success closes the breaker. Any other error falls back for that call only, as before.

    Degraded mode never starts below what Redis counted: each count Redis returns is kept
    as a floor for the key's shard in this window, so falling back (by the breaker or an
    outage) does not give a principal its attempts back in this process. A key this
    process has not seen this window starts from 0 (or its shard's count): the limit in
    degraded mode is per API process.

    ``SHARDS`` is 2**16 (#211 review): floors let an attacker fill shards while Redis is
    up, so that legitimate first attempts are refused once the breaker opens. With 4096
    shards, 1000 attacker keys at count 11 refused 20.95% of first attempts and 4096 keys
    63.69%. Two ``array('I')`` per window length, 256 KiB each, raise that cost 16-fold.
    """

    SHARDS = 2**16
    BREAKER_SECONDS = 10.0

    def __init__(self, name: str = "login") -> None:
        self._name = name  # the breaker metric's label
        # window length -> (count per shard, window index each count belongs to)
        self._tables: dict[int, tuple[array[int], array[int]]] = {}
        # Keyed hash with a per-process secret: shard collisions cannot be precomputed.
        self._salt = os.urandom(16)
        # Shards are shared by request threads: each read-modify-write holds this lock.
        self._local = threading.Lock()
        # Monotonic time until which Redis is skipped; 0 while the breaker is closed.
        self._open_until = 0.0
        self._breaker = threading.Lock()

    def _redis(self) -> Any | None:
        """The Redis client, or None while the breaker is open (count locally)."""
        from quantlab_api.jobs import redis_conn

        now = time.monotonic()
        with self._breaker:
            if now < self._open_until:
                return None
            if self._open_until:
                # Its window is over: this call is the one probe. Until it succeeds or
                # fails, every other call stays local, as though it were still open.
                self._open_until = now + self.BREAKER_SECONDS
        return redis_conn()

    def _redis_failed(self, exc: Exception) -> None:
        import redis

        from quantlab_api.observability import RATE_LIMITER_BREAKER_OPEN, log

        # Unreachable or not answering: OSError covers the socket-level ones redis-py
        # does not wrap. Anything else (a bad reply, say) is not an outage.
        if not isinstance(
            exc, (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError, OSError)
        ):
            return
        # The gauge and the log are written under the lock that decides the transition, so
        # an open and a close that interleave cannot leave the gauge at 1 while closed.
        with self._breaker:
            opening = not self._open_until
            self._open_until = time.monotonic() + self.BREAKER_SECONDS
            if opening:
                RATE_LIMITER_BREAKER_OPEN.labels(self._name).set(1)
                log.warning(
                    "rate_limiter_breaker_open", seconds=self.BREAKER_SECONDS, error=str(exc)
                )

    def _redis_answered(self) -> None:
        if not self._open_until:  # closed already: the usual case, without the lock
            return
        from quantlab_api.observability import RATE_LIMITER_BREAKER_OPEN, log

        with self._breaker:
            if self._open_until:
                self._open_until = 0.0
                RATE_LIMITER_BREAKER_OPEN.labels(self._name).set(0)
                log.info("rate_limiter_breaker_closed")

    def _table(self, window: int) -> tuple[array[int], array[int]]:
        table = self._tables.get(window)
        if table is None:
            empty = array("I", [0]) * self.SHARDS
            table = self._tables.setdefault(window, (empty, array("I", empty)))
        return table

    @staticmethod
    def _bucket(key: str, window: int) -> str:
        return f"ql:rl:{key}:{int(time.time() // window)}"

    def _shard(self, key: str) -> int:
        digest = hashlib.blake2b(key.encode(), key=self._salt, digest_size=8).digest()
        return int.from_bytes(digest) % self.SHARDS

    def _local_slot(self, key: str, window: int) -> tuple[array[int], int]:
        """The key's shard counts and index, reset if they belong to an earlier window.
        Called with ``self._local`` held."""
        counts, windows = self._table(window)
        i, w = self._shard(key), int(time.time() // window)
        if windows[i] != w:
            windows[i], counts[i] = w, 0
        return counts, i

    def _local_reserve(self, key: str, limit: int, window: int) -> bool:
        with self._local:
            counts, i = self._local_slot(key, window)
            counts[i] += 1
            return counts[i] <= limit

    def _local_floor(self, key: str, window: int, count: int) -> None:
        # Only ever raised: a lower count from Redis (one this shard has passed, or another
        # key sharing it) never gives back what was counted here.
        with self._local:
            counts, i = self._local_slot(key, window)
            if count > counts[i]:
                counts[i] = count

    def reserve(self, key: str, limit: int, window: int = 60) -> bool:
        """Atomically count one attempt; False if it exceeds ``limit`` in this window."""
        return bool(self.reserve_receipt(key, limit, window))

    def reserve_receipt(self, key: str, limit: int, window: int = 60) -> Reservation:
        """``reserve``, with a receipt saying where the attempt was counted: ``refund``
        gives it back there and only there (#211 review)."""
        bucket = self._bucket(key, window)
        try:
            r = self._redis()
            if r is None:
                return Reservation(self._local_reserve(key, limit, window), bucket, window, False)
            count = int(r.incr(bucket))
            if count == 1:
                r.expire(bucket, window + 1)
        except Exception as exc:
            self._redis_failed(exc)
            return Reservation(self._local_reserve(key, limit, window), bucket, window, False)
        self._redis_answered()
        self._local_floor(key, window, count)
        return Reservation(count <= limit, bucket, window, True)

    def refund(self, receipt: Reservation) -> None:
        """Give back one reserved attempt (e.g. the login succeeded), where it was counted.

        A local count is never given back: a shard may be shared by several principals.
        Redis gets a DECR only for an attempt Redis counted, in that attempt's bucket. A
        DECR of a bucket Redis no longer has (flushed, or expired at the window's end)
        would create it at -1 with no TTL; it is given back at once and bounded instead.
        """
        if not receipt.in_redis:
            return
        try:
            r = self._redis()
            if r is None:
                return  # the breaker is open: degraded mode ignores refunds
            if int(r.decr(receipt.bucket)) < 0:
                r.incr(receipt.bucket)
                r.expire(receipt.bucket, receipt.window + 1)
        except Exception as exc:
            self._redis_failed(exc)
            return
        self._redis_answered()

    def count(self, key: str, window: int = 60) -> int:
        try:
            r = self._redis()
            if r is not None:
                n = int(r.get(self._bucket(key, window)) or 0)
                self._redis_answered()
                return n
        except Exception as exc:
            self._redis_failed(exc)
        with self._local:
            counts, windows = self._table(window)
            i = self._shard(key)
            return counts[i] if windows[i] == int(time.time() // window) else 0

    def check(self, key: str, limit: int, window: int = 60) -> None:
        if not self.reserve(key, limit, window):
            raise ApiError(429, "rate_limited", "Too many attempts; try again later")

    def reset(self) -> None:
        self._tables = {}
        self._open_until = 0.0


login_limiter = RateLimiter()


# ------------------------------------------------------------------- device cookies
# A browser that has signed in to an account before keeps a signed "device cookie". A
# login that presents one for the submitted email is throttled per device instead of by
# the account-wide failure budget, so an attacker who spends that budget from several
# addresses cannot lock the owner out of a browser they already use (ADR-0008, #55).
# The cookie is used only to choose a throttling budget, never to authenticate.

DEVICE_COOKIE = "quantlab_device"
# Over TLS the name carries the __Secure- prefix (browsers then require Secure). Not
# __Host-: that prefix requires Path=/, and this cookie is scoped to the auth routes.
DEVICE_COOKIE_SECURE = "__Secure-quantlab_device"
DEVICE_COOKIE_PATH = "/api/v1/auth"
DEVICE_MAX_AGE = 90 * 24 * 3600
# Failures one device may make (per hour) before it loses its exemption.
DEVICE_FAILURES = 10
DEVICE_WINDOW = 3600
_DEVICE_CONTEXT = b"quantlab/device-cookie/v1"


def _device_key() -> bytes:
    """HKDF-SHA256 (RFC 5869) of the JWT signing key, with its own context label: the
    device cookie never shares a key with the session tokens."""
    ikm = get_settings().signing_key.encode()
    prk = hmac.new(b"quantlab-hkdf-salt", ikm, hashlib.sha256).digest()
    return hmac.new(prk, _DEVICE_CONTEXT + b"\x01", hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def device_cookie_name() -> str:
    return DEVICE_COOKIE_SECURE if get_settings().cookie_secure else DEVICE_COOKIE


def device_token(user: User, device_id: str, now: float | None = None) -> str:
    payload = {
        "d": device_id,
        "e": _email_hash(user.email),
        "t": int(time.time() if now is None else now),
        "v": user.token_version,
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(_device_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def trusted_device(request: Request, email: str, user: User | None) -> str | None:
    """The device id of a valid device cookie for this account, else None.

    Valid means: the signature verifies (constant-time), it was issued for this email,
    it is younger than DEVICE_MAX_AGE, and the account's token version has not changed
    since (logging out everywhere revokes every device).
    """
    raw = request.cookies.get(device_cookie_name())
    if not raw or user is None or "." not in raw:
        return None
    body, _, sig = raw.partition(".")
    try:
        expected = hmac.new(_device_key(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(sig)):
            return None
        claims = json.loads(_unb64(body))
        device_id, issued = str(claims["d"]), int(claims["t"])
        email_hash, version = str(claims["e"]), int(claims["v"])
    except (ValueError, KeyError, TypeError):
        return None
    if not hmac.compare_digest(email_hash, _email_hash(email)):
        return None
    age = time.time() - issued
    # Not issued in the future (beyond clock skew), not older than the maximum age.
    if age < -60 or age > DEVICE_MAX_AGE or version != user.token_version:
        return None
    return device_id


def set_device_cookie(response: Response, user: User, device_id: str | None) -> None:
    """Issue (or refresh) the device cookie after a successful login."""
    response.set_cookie(
        device_cookie_name(),
        device_token(user, device_id or secrets.token_hex(16)),
        max_age=DEVICE_MAX_AGE,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="strict",
        path=DEVICE_COOKIE_PATH,
    )
