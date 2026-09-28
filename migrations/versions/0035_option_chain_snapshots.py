"""daily option chain snapshots (Phase 100, D119)

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-28

One row per option contract per captured vendor quote. The platform has
never had historical option prices - every options backtest priced its
legs with Black-Scholes from the underlying - and nothing free publishes
past chains, so the only way to own that history is to start recording it.
This table is where it goes.

**Sized for a metered database, on purpose.** Only expiries within 60 DTE
and strikes within +-30% of spot are written, once per underlying per
trading day (a later capture for the same trade date replaces the earlier
one). Measured against the real TQQQ Cboe document of 2026-09-25: 908 of
1,688 contracts pass the filter, so ~229k rows a year per underlying.

**Natural primary key (`contract_symbol`, `as_of`)**, the uniqueness the
design requires - one vendor quote at one vendor timestamp is one row. A
surrogate UUID would cost a second unique index and ~40 bytes a row for no
identity the natural key lacks, the same reasoning `market_data_bars`
(0016) used. The secondary index serves the only two read shapes: "the
snapshot for this underlying on this date" and "which dates exist".

Nullable quote columns: a field the vendor did not send is NULL, never 0.

DOWNGRADE drops the table and every snapshot in it. They cannot be
re-fetched: the vendor publishes the current chain only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "option_chain_snapshots",
        sa.Column("contract_symbol", sa.String(32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("underlying", sa.String(32), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("underlying_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("right", sa.String(4), nullable=False),
        sa.Column("strike", sa.Numeric(20, 6), nullable=False),
        sa.Column("bid", sa.Numeric(20, 6), nullable=True),
        sa.Column("ask", sa.Numeric(20, 6), nullable=True),
        sa.Column("last", sa.Numeric(20, 6), nullable=True),
        sa.Column("iv", sa.Numeric(20, 8), nullable=True),
        sa.Column("delta", sa.Numeric(20, 8), nullable=True),
        sa.Column("gamma", sa.Numeric(20, 8), nullable=True),
        sa.Column("theta", sa.Numeric(20, 8), nullable=True),
        sa.Column("vega", sa.Numeric(20, 8), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("open_interest", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("contract_symbol", "as_of", name="pk_option_chain_snapshots"),
        sa.CheckConstraint("\"right\" IN ('call', 'put')", name="ck_option_snapshot_right"),
    )
    op.create_index(
        "ix_option_snapshots_underlying_trade_date",
        "option_chain_snapshots",
        ["underlying", "trade_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_option_snapshots_underlying_trade_date", table_name="option_chain_snapshots"
    )
    op.drop_table("option_chain_snapshots")
