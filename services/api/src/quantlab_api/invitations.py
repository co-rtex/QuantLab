"""Workspace invitations: consent-based membership (#5).

An owner invites an email address. Membership exists only after the invitee accepts with
the invitation's token, which reaches them through a ``Notifier`` (a local outbox here;
an email or chat adapter in a deployment that has one).

Security properties:
* Creating an invitation never reads the users table, so the response and its timing are
  identical whether or not the address belongs to an account (no enumeration).
* Registration does not verify email ownership, so acceptance requires both the token
  (proof that the delivered message was received) and a signed-in account with the
  invited address. Pre-registering someone's address is not enough to join as them.
* Tokens are 256-bit random values. Only their SHA-256 is stored (they have full
  entropy, so a slow hash adds nothing). They expire, and every transition (accept,
  decline, revoke) is a conditional UPDATE on ``status = 'pending'``, so a token works at
  most once even under concurrent requests.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

import structlog
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from quantlab_api.config import get_settings
from quantlab_api.db import Base
from quantlab_api.models import new_id, utcnow

# Alembic's env.py imports this module to see the table, before the API creates the
# Prometheus multiprocess directory, so it must not import quantlab_api.observability
# (which registers metrics on import). Same logger name, without the metrics.
log = structlog.get_logger("quantlab")

INVITATION_TTL = timedelta(days=7)
# Abuse bounds: invitations created per hour by one owner and into one workspace, and
# pending invitations a workspace may hold at once.
INVITES_PER_HOUR_PER_INVITER = 20
INVITES_PER_HOUR_PER_WORKSPACE = 50
MAX_PENDING_PER_WORKSPACE = 100

STATUSES = ("pending", "accepted", "declined", "revoked")
ROLES = ("viewer", "editor", "owner")


class Invitation(Base):
    __tablename__ = "workspace_invitations"
    __table_args__ = (
        CheckConstraint(f"status IN {STATUSES}", name="status"),
        CheckConstraint(f"role IN {ROLES}", name="role"),
        # One live invitation per address and workspace; re-inviting revokes the old one.
        Index(
            "uq_invitation_pending",
            "workspace_id",
            "email",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=new_id)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    responded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def new_token() -> tuple[str, str]:
    """(token for the invitee, hash to store)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------- schemas
class InvitationIn(BaseModel):
    email: EmailStr
    role: Literal["viewer", "editor", "owner"] = "viewer"


class InvitationOut(BaseModel):
    """What the inviting owner sees. Never says whether the address has an account."""

    id: uuid.UUID
    email: str
    role: str
    status: str
    created_at: datetime
    expires_at: datetime


class InvitationTokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=128)


class InvitationPreview(BaseModel):
    """What the invitee sees before answering."""

    workspace_id: uuid.UUID
    workspace_name: str
    role: str
    invited_by: str | None
    expires_at: datetime


class MemberRoleIn(BaseModel):
    role: Literal["viewer", "editor", "owner"]


class MailboxItem(BaseModel):
    to: str
    subject: str
    accept_path: str
    sent_at: datetime


# ----------------------------------------------------------------------- notification
@dataclass(frozen=True)
class InvitationMessage:
    to: str
    workspace_name: str
    inviter_name: str
    role: str
    # Path on the web app; the token is in the fragment so it never reaches server logs
    # or Referer headers.
    accept_path: str
    expires_at: str
    sent_at: str


class Notifier(Protocol):
    def send_invitation(self, message: InvitationMessage) -> None: ...


class OutboxNotifier:
    """Writes each message as a JSON file under ``$QUANTLAB_DATA_DIR/outbox`` (0600).

    The local-development delivery channel: no external service. A deployment with email
    replaces ``notifier`` with an adapter implementing ``Notifier``.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root or get_settings().data_dir / "outbox"

    def send_invitation(self, message: InvitationMessage) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / f"{message.sent_at.replace(':', '')}-{uuid.uuid4().hex}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"kind": "invitation", **asdict(message)}, f)
        log.info("invitation_queued", outbox=str(path))

    def messages_for(self, email: str) -> list[dict[str, str]]:
        if not self.root.exists():
            return []
        out = []
        for p in sorted(self.root.glob("*.json")):
            try:
                m = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if m.get("to") == email:
                out.append(m)
        return out


notifier: Notifier = OutboxNotifier()


def expiry() -> datetime:
    return datetime.now(UTC) + INVITATION_TTL
