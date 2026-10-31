"""workspace invitations (consent-based membership, #5)

Owners invite an email address; membership exists only after the invitee accepts with a
single-use token. Only the token's SHA-256 is stored. At most one pending invitation per
workspace and address (partial unique index).

Revision ID: 7c3e5a1d9b20
Revises: 4f1c2a9e7b10
Create Date: 2026-10-31 00:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c3e5a1d9b20"
down_revision: str | Sequence[str] | None = "4f1c2a9e7b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "workspace_invitations"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=True),
        sa.Column("responded_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('viewer', 'editor', 'owner')", name=op.f(f"ck_{TABLE}_role")),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'revoked')",
            name=op.f(f"ck_{TABLE}_status"),
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"],
            ["users.id"],
            name=op.f(f"fk_{TABLE}_invited_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["responded_by"],
            ["users.id"],
            name=op.f(f"fk_{TABLE}_responded_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f(f"fk_{TABLE}_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{TABLE}")),
        sa.UniqueConstraint("token_hash", name=op.f(f"uq_{TABLE}_token_hash")),
    )
    op.create_index(op.f(f"ix_{TABLE}_workspace_id"), TABLE, ["workspace_id"], unique=False)
    op.create_index(
        "uq_invitation_pending",
        TABLE,
        ["workspace_id", "email"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("uq_invitation_pending", table_name=TABLE)
    op.drop_index(op.f(f"ix_{TABLE}_workspace_id"), table_name=TABLE)
    op.drop_table(TABLE)
