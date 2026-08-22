"""initial: users, roles, assets, brokers

Revision ID: 0001
Revises:
Create Date: 2026-08-22

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False, unique=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False, unique=True),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "role_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("roles.id"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    asset_class_enum = postgresql.ENUM(
        "equity", "etf", "option", "future", "crypto", "forex", name="assetclass", create_type=False
    )
    asset_class_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("asset_class", asset_class_enum, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("symbol", "asset_class", name="uq_asset_symbol_class"),
    )
    op.create_index("ix_assets_symbol", "assets", ["symbol"])

    broker_kind_enum = postgresql.ENUM("paper", "live", name="brokerkind", create_type=False)
    broker_kind_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "brokers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("kind", broker_kind_enum, nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("brokers")
    op.drop_table("assets")
    op.drop_table("users")
    op.drop_table("roles")
    postgresql.ENUM(name="brokerkind").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="assetclass").drop(op.get_bind(), checkfirst=True)
