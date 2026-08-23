"""orders, fills - append-only order/fill persistence

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    side_enum = postgresql.ENUM("buy", "sell", name="side", create_type=False)
    side_enum.create(op.get_bind(), checkfirst=True)

    order_status_enum = postgresql.ENUM("filled", "rejected", name="orderstatus", create_type=False)
    order_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "broker_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("brokers.id"), nullable=False
        ),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", side_enum, nullable=False),
        sa.Column("quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("estimated_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("stop_price", sa.Numeric(24, 8), nullable=True),
        sa.Column("status", order_status_enum, nullable=False),
        sa.Column("risk_block_reason", sa.String(length=64), nullable=True),
        sa.Column("risk_detail", sa.String(length=500), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_orders_symbol", "orders", ["symbol"])

    op.create_table(
        "fills",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("fill_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("fills")
    op.drop_table("orders")
    postgresql.ENUM(name="orderstatus").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="side").drop(op.get_bind(), checkfirst=True)
