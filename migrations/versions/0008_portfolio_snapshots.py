"""portfolio_snapshots, portfolio_snapshot_positions - append-only persisted
history of compute_portfolio_snapshot() output (docs/DECISIONS.md D027)

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-28

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "broker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("brokers.id"),
            nullable=False,
        ),
        sa.Column(
            "captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("cash", sa.Numeric(24, 8), nullable=False),
        sa.Column("total_equity", sa.Numeric(24, 8), nullable=False),
        sa.Column("total_unrealized_pnl", sa.Numeric(24, 8), nullable=False),
        sa.Column("total_realized_pnl", sa.Numeric(24, 8), nullable=False),
    )
    op.create_index(
        "ix_portfolio_snapshots_broker_captured",
        "portfolio_snapshots",
        ["broker_id", "captured_at"],
    )

    op.create_table(
        "portfolio_snapshot_positions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("portfolio_snapshots.id"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("avg_cost", sa.Numeric(24, 8), nullable=False),
        sa.Column("current_value", sa.Numeric(24, 8), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(24, 8), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(24, 8), nullable=False),
    )
    op.create_index(
        "ix_portfolio_snapshot_positions_snapshot",
        "portfolio_snapshot_positions",
        ["snapshot_id"],
    )
    op.create_index(
        "ix_portfolio_snapshot_positions_symbol",
        "portfolio_snapshot_positions",
        ["symbol"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_snapshot_positions_symbol", table_name="portfolio_snapshot_positions"
    )
    op.drop_index(
        "ix_portfolio_snapshot_positions_snapshot", table_name="portfolio_snapshot_positions"
    )
    op.drop_table("portfolio_snapshot_positions")
    op.drop_index("ix_portfolio_snapshots_broker_captured", table_name="portfolio_snapshots")
    op.drop_table("portfolio_snapshots")
