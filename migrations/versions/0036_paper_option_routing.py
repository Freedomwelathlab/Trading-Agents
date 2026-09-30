"""paper option routing and the options paper bot (Phase 102, D122)

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-29

**What this stores.**

    options_bots          one options robot: underlying, structure type,
                          target delta, DTE band, profit target / stop %,
                          max concurrent positions, capital per trade, and
                          the same PENDING_APPROVAL -> ACTIVE human gate
    options_bot_runs      one row per bot cycle: what it checked, opened,
                          closed, or WHY it did nothing
    option_structures     one paper option position held as a unit (a
                          single option or a defined-risk multi-leg
                          structure), with the capital it reserves
    option_orders         every option order this system decided on,
                          filled or refused, labelled with the quote source
                          and quote time it was priced from
    option_fills          one row per leg of a filled order: the vendor's
                          bid/ask/mid and the MODELLED fill price
    options_bot_trades    the bot's own ledger over the structures it
                          opened, for the per-structure learning summary

**Why not `orders` / `broker_positions`.** Those hold single-symbol equity
orders and quantities that every portfolio view, the autotrade engine and
the deployment runner value by the symbol's stored bar. An option contract
has no bar, and a held row there with no bar fails those cycles closed
("no price was fabricated"). The option book is therefore kept beside the
equity book, sharing only the cash in `broker_accounts`.

**Statuses are VARCHARs** (same reasoning as 0031): each new refusal reason
would otherwise be an `ALTER TYPE`.

**No circular foreign key.** An order points at its structure; the
structure does not point back at its orders (query `option_orders` by
`structure_id`).

DOWNGRADE drops the six tables. Cash already moved in `broker_accounts` by
option fills is NOT reversed - it is the paper account's real history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0036"
down_revision = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)


def _ts(name: str, *, nullable: bool = True, default_now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if default_now else None,
    )


def upgrade() -> None:
    op.create_table(
        "options_bots",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("owner_user_id", _UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column(
            "broker_id", _UUID, sa.ForeignKey("brokers.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("underlying", sa.String(32), nullable=False),
        sa.Column("structure_type", sa.String(24), nullable=False),
        sa.Column("target_delta", sa.Numeric(6, 4), nullable=False),
        sa.Column("dte_min", sa.Integer(), nullable=False),
        sa.Column("dte_max", sa.Integer(), nullable=False),
        sa.Column("spread_width", sa.Numeric(12, 4), nullable=True),
        sa.Column("profit_target_pct", sa.Numeric(8, 4), nullable=False),
        sa.Column("stop_pct", sa.Numeric(8, 4), nullable=False),
        sa.Column("max_concurrent_positions", sa.Integer(), nullable=False),
        sa.Column("capital_per_trade", sa.Numeric(24, 8), nullable=False),
        sa.Column(
            "requested_by_user_id", _UUID, sa.ForeignKey("users.id", ondelete="SET NULL")
        ),
        sa.Column("approved_by_user_id", _UUID, sa.ForeignKey("users.id", ondelete="SET NULL")),
        _ts("approved_at"),
        sa.Column("paused_reason", sa.String(500), nullable=True),
        _ts("stopped_at"),
        _ts("last_evaluated_at"),
        _ts("created_at", nullable=False, default_now=True),
        _ts("updated_at", nullable=False, default_now=True),
        sa.CheckConstraint(
            "status IN ('pending_approval', 'active', 'paused', 'stopped')",
            name="ck_options_bot_status",
        ),
        sa.CheckConstraint("dte_min >= 0 AND dte_max >= dte_min", name="ck_options_bot_dte"),
        sa.CheckConstraint(
            "target_delta > 0 AND target_delta < 1", name="ck_options_bot_target_delta"
        ),
        sa.CheckConstraint("capital_per_trade > 0", name="ck_options_bot_capital"),
        sa.CheckConstraint("max_concurrent_positions >= 1", name="ck_options_bot_max_concurrent"),
    )
    op.create_index("ix_options_bots_owner", "options_bots", ["owner_user_id"])

    op.create_table(
        "options_bot_runs",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "bot_id", _UUID, sa.ForeignKey("options_bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("status", sa.String(40), nullable=False),
        _ts("started_at", nullable=False),
        _ts("completed_at"),
        sa.Column("positions_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trades_opened", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trades_closed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detail", sa.Text(), nullable=True),
        _ts("created_at", nullable=False, default_now=True),
    )
    op.create_index(
        "ix_options_bot_runs_bot_started", "options_bot_runs", ["bot_id", "started_at"]
    )

    op.create_table(
        "option_structures",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "broker_id", _UUID, sa.ForeignKey("brokers.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "options_bot_id", _UUID, sa.ForeignKey("options_bots.id", ondelete="SET NULL")
        ),
        sa.Column("structure_type", sa.String(24), nullable=False),
        sa.Column("underlying", sa.String(32), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("legs", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("entry_net_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("max_loss", sa.Numeric(24, 8), nullable=False),
        sa.Column("max_profit", sa.Numeric(24, 8), nullable=True),
        sa.Column("capital_reserved", sa.Numeric(24, 8), nullable=False),
        sa.Column("quote_source", sa.String(64), nullable=False),
        _ts("quote_as_of", nullable=False),
        _ts("opened_at", nullable=False),
        _ts("closed_at"),
        sa.Column("exit_net_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("settlement_underlying_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(24, 8), nullable=True),
        sa.Column("close_reason", sa.String(32), nullable=True),
        _ts("created_at", nullable=False, default_now=True),
        sa.CheckConstraint(
            "status IN ('open', 'closed', 'settled')", name="ck_option_structure_status"
        ),
        sa.CheckConstraint("quantity > 0", name="ck_option_structure_quantity"),
        sa.CheckConstraint("capital_reserved >= 0", name="ck_option_structure_capital"),
    )
    op.create_index(
        "ix_option_structures_broker_status", "option_structures", ["broker_id", "status"]
    )

    op.create_table(
        "option_orders",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "broker_id", _UUID, sa.ForeignKey("brokers.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "structure_id", _UUID, sa.ForeignKey("option_structures.id", ondelete="SET NULL")
        ),
        sa.Column(
            "options_bot_run_id",
            _UUID,
            sa.ForeignKey("options_bot_runs.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "submitted_by_user_id", _UUID, sa.ForeignKey("users.id", ondelete="SET NULL")
        ),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("structure_type", sa.String(24), nullable=False),
        sa.Column("structure_key", sa.String(160), nullable=False),
        sa.Column("underlying", sa.String(32), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("block_reason", sa.String(64), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("net_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("cash_change", sa.Numeric(24, 8), nullable=True),
        sa.Column("max_loss_per_contract", sa.Numeric(24, 8), nullable=True),
        sa.Column("quote_source", sa.String(64), nullable=False),
        _ts("quote_as_of", nullable=False),
        sa.Column("fill_haircut_k", sa.Numeric(6, 4), nullable=False),
        _ts("created_at", nullable=False, default_now=True),
        sa.CheckConstraint("action IN ('open', 'close')", name="ck_option_order_action"),
        sa.CheckConstraint("status IN ('filled', 'rejected')", name="ck_option_order_status"),
    )
    op.create_index(
        "ix_option_orders_broker_created", "option_orders", ["broker_id", "created_at"]
    )

    op.create_table(
        "option_fills",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "order_id",
            _UUID,
            sa.ForeignKey("option_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("contract_symbol", sa.String(32), nullable=False),
        sa.Column("right", sa.String(4), nullable=False),
        sa.Column("strike", sa.Numeric(20, 6), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("bid", sa.Numeric(20, 6), nullable=False),
        sa.Column("ask", sa.Numeric(20, 6), nullable=False),
        sa.Column("mid", sa.Numeric(20, 6), nullable=False),
        sa.Column("fill_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("quote_source", sa.String(64), nullable=False),
        _ts("quote_as_of", nullable=False),
        _ts("filled_at", nullable=False),
        sa.CheckConstraint("\"right\" IN ('call', 'put')", name="ck_option_fill_right"),
        sa.CheckConstraint("side IN ('buy', 'sell')", name="ck_option_fill_side"),
    )
    op.create_index("ix_option_fills_order", "option_fills", ["order_id"])

    op.create_table(
        "options_bot_trades",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "bot_id", _UUID, sa.ForeignKey("options_bots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "structure_id",
            _UUID,
            sa.ForeignKey("option_structures.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "open_run_id", _UUID, sa.ForeignKey("options_bot_runs.id", ondelete="SET NULL")
        ),
        sa.Column(
            "close_run_id", _UUID, sa.ForeignKey("options_bot_runs.id", ondelete="SET NULL")
        ),
        sa.Column("structure_type", sa.String(24), nullable=False),
        sa.Column("underlying", sa.String(32), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("target_delta", sa.Numeric(6, 4), nullable=False),
        sa.Column("entry_delta", sa.Numeric(10, 6), nullable=True),
        sa.Column("entry_net_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("max_loss", sa.Numeric(24, 8), nullable=False),
        sa.Column("profit_target_pct", sa.Numeric(8, 4), nullable=False),
        sa.Column("stop_pct", sa.Numeric(8, 4), nullable=False),
        sa.Column("peak_pnl_pct", sa.Numeric(12, 4), nullable=True),
        sa.Column("trough_pnl_pct", sa.Numeric(12, 4), nullable=True),
        _ts("opened_at", nullable=False),
        _ts("closed_at"),
        sa.Column("exit_net_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("exit_reason", sa.String(32), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(24, 8), nullable=True),
        sa.Column("return_on_risk", sa.Numeric(12, 6), nullable=True),
        _ts("created_at", nullable=False, default_now=True),
    )
    op.create_index("ix_options_bot_trades_bot", "options_bot_trades", ["bot_id", "opened_at"])


def downgrade() -> None:
    op.drop_index("ix_options_bot_trades_bot", table_name="options_bot_trades")
    op.drop_table("options_bot_trades")
    op.drop_index("ix_option_fills_order", table_name="option_fills")
    op.drop_table("option_fills")
    op.drop_index("ix_option_orders_broker_created", table_name="option_orders")
    op.drop_table("option_orders")
    op.drop_index("ix_option_structures_broker_status", table_name="option_structures")
    op.drop_table("option_structures")
    op.drop_index("ix_options_bot_runs_bot_started", table_name="options_bot_runs")
    op.drop_table("options_bot_runs")
    op.drop_index("ix_options_bots_owner", table_name="options_bots")
    op.drop_table("options_bots")
