"""users.failed_login_count / users.locked_until - persisted failed-login
lockout for POST /auth/login (docs/DECISIONS.md D049)

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-30

`failed_login_count` is NOT NULL with `server_default='0'` because every
row that already exists has, by definition, no recorded run of consecutive
failures - zero is a fact about those rows, not a placeholder. The default
stays on the column so an INSERT from any code path that predates the model
change (scripts/seed_e2e.py's direct SQL, a manual first-admin insert per
D010) still lands a truthful value instead of failing.

`locked_until` is nullable on purpose: NULL means "never locked", which is
genuinely different from a timestamp in the past ("was locked, expired"),
and the login route relies on that distinction being preserved rather than
collapsed into a sentinel date.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "users",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_count")
