"""broker_grants - explicit per-user, per-broker access grants

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "broker_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "broker_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("brokers.id"), nullable=False
        ),
        sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "broker_id", name="uq_broker_grant_user_broker"),
    )


def downgrade() -> None:
    op.drop_table("broker_grants")
