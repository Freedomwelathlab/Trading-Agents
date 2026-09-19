"""autotrade bots: operator-configured intraday robots (Phase 81, D098)

Revision ID: 0031
Revises: 0030
Create Date: 2026-09-19

**What this stores.**

    autotrade_bots         one robot: its inputs (symbols, session, trade
                           limits, capital per trade, setups, stop / take-
                           profit rules) and its approval lifecycle
    autotrade_bot_runs     one row per cycle: scanned / found / opened /
                           closed, or WHY nothing happened
    autotrade_bot_trades   one row per position the bot opened, with the
                           bracket it is managed under and, once closed,
                           the exit and its outcome in R

**Why a bot is not a strategy deployment.** A `StrategyDeployment` runs one
closed-vocabulary strategy definition on daily bars, long/flat, sized by
that definition. The bot runs the INTRADAY setup detectors
(`backtesting/setups.py`) on 5-minute bars across many symbols, ranks the
signals, and manages a bracket (stop, trailing stop, take profit, trailing
take profit, session-end flat) on every open position each cycle. The
order path is the same one (`oms.persistence.submit_trade_and_record`);
everything above it is different, so it gets its own tables rather than
overloading the deployment ones with nullable columns.

**Same human gate.** `status` starts at `pending_approval` and only an
explicit, separately-permissioned approval makes it `active`. There is no
row shape that is active on creation.

**Statuses are VARCHARs, not Postgres enum types.** The run-status list is
the one most likely to grow (each new refusal reason is a new member), and
each addition to a native enum is an `ALTER TYPE` migration. The ORM still
exposes typed Python enums; only the storage differs from the deployment
tables.

**`symbols` is a snapshot.** The bot records `watchlist_id` for provenance
but trades the array it was created with — adding a symbol to a watchlist
must not silently put it into a running robot's rotation.

DOWNGRADE
---------
Drops the three tables. Orders the bot placed are ordinary `orders` rows
and are NOT touched — they are the account's real history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0031"
down_revision = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "autotrade_bots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "broker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("brokers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column(
            "watchlist_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("watchlists.id", ondelete="SET NULL"),
        ),
        sa.Column("symbols", postgresql.ARRAY(sa.String(length=32)), nullable=False),
        sa.Column("market_type", sa.String(length=16), nullable=False, server_default="regular"),
        sa.Column("bar_interval", sa.String(length=8), nullable=False, server_default="5m"),
        sa.Column("max_trades_per_session", sa.Integer(), nullable=False),
        sa.Column("max_trades_per_day", sa.Integer(), nullable=False),
        sa.Column("capital_per_trade", sa.Numeric(24, 8), nullable=False),
        sa.Column("strategy_mode", sa.String(length=8), nullable=False, server_default="auto"),
        sa.Column("setups", postgresql.ARRAY(sa.String(length=32)), nullable=False),
        sa.Column("min_score", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("stop_loss_mode", sa.String(length=8), nullable=False, server_default="auto"),
        sa.Column("stop_loss_max_pct", sa.Numeric(8, 4)),
        sa.Column("trailing_stop_pct", sa.Numeric(8, 4)),
        sa.Column("take_profit_mode", sa.String(length=8), nullable=False, server_default="auto"),
        sa.Column("take_profit_min_pct", sa.Numeric(8, 4)),
        sa.Column("trailing_take_profit_pct", sa.Numeric(8, 4)),
        sa.Column("news_blackout_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "requested_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "approved_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("paused_reason", sa.String(length=500)),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_autotrade_bots_status", "autotrade_bots", ["status"])
    op.create_index("ix_autotrade_bots_owner", "autotrade_bots", ["owner_user_id"])

    op.create_table(
        "autotrade_bot_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("autotrade_bots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("symbols_scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signals_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signals_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trades_opened", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trades_closed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "setups_active",
            postgresql.ARRAY(sa.String(length=32)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("detail", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_autotrade_bot_runs_bot_started", "autotrade_bot_runs", ["bot_id", "started_at"]
    )

    op.create_table(
        "autotrade_bot_trades",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("autotrade_bots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "open_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("autotrade_bot_runs.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "close_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("autotrade_bot_runs.id", ondelete="SET NULL"),
        ),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("setup_name", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column(
            "evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column(
            "entry_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="SET NULL"),
        ),
        sa.Column("entry_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("initial_stop_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("stop_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("take_profit_price", sa.Numeric(20, 6)),
        sa.Column("peak_price", sa.Numeric(20, 6), nullable=False),
        sa.Column(
            "take_profit_armed", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "exit_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="SET NULL"),
        ),
        sa.Column("exit_price", sa.Numeric(20, 6)),
        sa.Column("exit_reason", sa.String(length=32)),
        sa.Column("realized_pnl", sa.Numeric(24, 8)),
        sa.Column("r_multiple", sa.Numeric(12, 4)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_autotrade_bot_trades_bot_open", "autotrade_bot_trades", ["bot_id", "closed_at"]
    )
    op.create_index(
        "ix_autotrade_bot_trades_bot_session",
        "autotrade_bot_trades",
        ["bot_id", "session_date"],
    )

    op.add_column(
        "orders",
        sa.Column(
            "autotrade_bot_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("autotrade_bot_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("orders", "autotrade_bot_run_id")
    op.drop_index("ix_autotrade_bot_trades_bot_session", table_name="autotrade_bot_trades")
    op.drop_index("ix_autotrade_bot_trades_bot_open", table_name="autotrade_bot_trades")
    op.drop_table("autotrade_bot_trades")
    op.drop_index("ix_autotrade_bot_runs_bot_started", table_name="autotrade_bot_runs")
    op.drop_table("autotrade_bot_runs")
    op.drop_index("ix_autotrade_bots_owner", table_name="autotrade_bots")
    op.drop_index("ix_autotrade_bots_status", table_name="autotrade_bots")
    op.drop_table("autotrade_bots")
