"""broker_accounts, broker_positions - persisted paper-broker state

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "broker_accounts",
        sa.Column(
            "broker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("brokers.id"),
            primary_key=True,
        ),
        sa.Column("cash", sa.Numeric(24, 8), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )

    op.create_table(
        "broker_positions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "broker_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("brokers.id"), nullable=False
        ),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.UniqueConstraint("broker_id", "symbol", name="uq_broker_position_broker_symbol"),
    )


def downgrade() -> None:
    op.drop_table("broker_positions")
    op.drop_table("broker_accounts")
