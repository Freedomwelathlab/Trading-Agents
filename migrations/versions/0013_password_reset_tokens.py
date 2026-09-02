"""password_reset_tokens - single-use, short-TTL password reset tokens for
POST /auth/password-reset/{request,confirm} (docs/DECISIONS.md D063)

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-02

`token_hash` stores a SHA-256 hex digest of the token, never the token
itself - the same principle as `users.hashed_password`. A stolen database
dump therefore yields no usable reset link. It is UNIQUE, which is both a
correctness guarantee (one row per issued token) and what makes redemption
an indexed single-row lookup rather than a scan.

SHA-256 rather than bcrypt here on purpose, and it is not a weakening: the
value being hashed is 32 bytes from `secrets.token_urlsafe`, not a
human-chosen password, so the slow-KDF property bcrypt exists for buys
nothing against a 256-bit search space. Bcrypt would also make redemption
impossible to index - every row carries its own salt, so finding "the row
matching this token" would mean bcrypt-verifying every outstanding row.

`user_id` is ON DELETE CASCADE. Unlike `orders.submitted_by_user_id`
(deliberately nullable so an order's audit trail survives its submitter),
a reset token has no independent meaning once its user is gone - it is a
capability to set that user's password, and it must not outlive them.
Note that this codebase does not delete users at all (deactivation via
`is_active=false` is the documented path, D013), so the cascade is a
structural guarantee rather than a routine code path.

`used_at` is nullable and means CONSUMED - either redeemed by this token's
own confirm call, or invalidated because a sibling token for the same user
was redeemed first. NULL is the only state in which a token is redeemable.
It is never cleared back to NULL.

`expires_at` is stored absolutely rather than derived from `created_at` +
a setting read at redemption time, so shortening or lengthening
AUTH_PASSWORD_RESET_TOKEN_TTL_MINUTES later cannot retroactively extend or
revoke a token that is already in someone's inbox.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Supports both hot paths that are not the unique-hash lookup: the
    # per-account request throttle (count this user's rows created inside
    # the window) and the sibling invalidation on redemption (find this
    # user's other unused rows).
    op.create_index(
        "ix_password_reset_tokens_user_created",
        "password_reset_tokens",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_password_reset_tokens_user_created", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
