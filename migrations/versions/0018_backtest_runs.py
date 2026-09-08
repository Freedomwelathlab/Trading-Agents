"""backtest_runs + backtest_equity_points + backtest_trades - persisted
results of running a StrategyVersion over a real historical bar window
(Phase 55)

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-08

This is what closes D025's honest limitation. The v1 backtest engine
(apps/api/app/backtesting/engine.py) computes a `BacktestResult` in memory,
returns it over HTTP, and throws it away - it persists nothing, so there is
no way to compare two runs, to re-read yesterday's numbers, or to say which
exact rule set produced a result. These three tables are that record.

**`backtest_runs.strategy_version_id` is ON DELETE RESTRICT.** Migration
0017 already named this FK as the reason `strategy_versions` is a separate
table at all: a run's input must be a specific frozen definition that
nothing can edit or delete after the fact. CASCADE would let tidying up a
strategy silently destroy real computed results; SET NULL would leave rows
asserting numbers nothing can explain. RESTRICT is the only option that
keeps a persisted result meaningful, and it costs nothing operationally -
there is no route in this phase, or planned in the next several, that
deletes a `StrategyVersion`.

The two child tables go the other way: `backtest_equity_points` and
`backtest_trades` are ON DELETE CASCADE, because a curve point or a round
trip has no meaning apart from the run that computed it - the same
relationship `watchlist_items` has to its watchlist.

`requested_by_user_id` is ON DELETE SET NULL, matching
`orders.submitted_by_user_id`, `emergency_stop_events.actor_user_id` and
`market_data_backfill_jobs.requested_by_user_id`: an operational record's
meaning survives the deletion of whoever requested it.

One new enum TYPE, created and dropped explicitly with
`postgresql.ENUM(..., create_type=False)` + `.create()` / `.drop()`,
following migration 0001's pattern rather than letting `sa.Enum` create it
implicitly as a side effect of `create_table` (which leaves the downgrade
with an orphaned type):

- `backtestrunstatus` (`pending`, `running`, `succeeded`, `failed`) - the
  same four names `backfilljobstatus` (migration 0016) uses, minus its
  `partial`. A single-symbol, single-window backtest either produced a full
  equity curve or it did not; there is no honest middle state for it to
  report, so the value is not carried over merely for symmetry.

`bar_interval` is a plain VARCHAR here for exactly the reason
`market_data_bars.bar_interval` is (migration 0016): it names the same
growing vocabulary (5m, 1h, ...), and growing it should be an application
change rather than a migration that mutates a type every existing row
depends on. Only `'1d'` is ever written by Phase 55.

Every metric column on `backtest_runs` is NULLABLE. A FAILED run computed
none of them, and writing a 0 would be a fabricated figure
(docs/TRADING_SAFETY.md's no-fabrication rule) - `NULL` says "not
computed", which is the fact. `error_detail` is where a failure's real
cause is recorded, most often an `InsufficientHistoryError` naming exactly
how many warmup bars were needed and how many exist.

`ix_backtest_runs_version_created` supports the only listing query there
is, `GET /strategies/{id}/versions/{id}/backtests` - "this version's runs,
newest first". `ix_backtest_trades_run` supports fetching one run's trades
for the detail response. `backtest_equity_points` needs no separate index:
its `(backtest_run_id, date)` unique constraint doubles as the index for
"this run's curve", and also guarantees one point per day per run.

Downgrade drops in dependency order - `backtest_trades` and
`backtest_equity_points` first (both reference `backtest_runs`), then
`backtest_runs` (which references `strategy_versions` and `users`), then
the enum type. Nothing else in the schema references any of the three, so
the round-trip is clean.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    backtest_run_status_enum = postgresql.ENUM(
        "pending",
        "running",
        "succeeded",
        "failed",
        name="backtestrunstatus",
        create_type=False,
    )
    backtest_run_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "backtest_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "strategy_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "requested_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("bar_interval", sa.String(length=8), nullable=False, server_default="1d"),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("starting_cash", sa.Numeric(20, 6), nullable=False),
        sa.Column("status", backtest_run_status_enum, nullable=False),
        sa.Column("final_equity", sa.Numeric(20, 6), nullable=True),
        sa.Column("total_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("max_drawdown_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("win_rate_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("num_trades", sa.Integer(), nullable=True),
        sa.Column("error_detail", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_backtest_runs_version_created",
        "backtest_runs",
        ["strategy_version_id", "created_at"],
    )

    op.create_table(
        "backtest_equity_points",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "backtest_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("equity", sa.Numeric(20, 6), nullable=False),
        sa.UniqueConstraint(
            "backtest_run_id", "date", name="uq_backtest_equity_point_run_date"
        ),
    )

    op.create_table(
        "backtest_trades",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "backtest_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("entry_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("exit_date", sa.Date(), nullable=True),
        sa.Column("exit_price", sa.Numeric(20, 6), nullable=True),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("return_pct", sa.Numeric(10, 4), nullable=True),
    )
    op.create_index("ix_backtest_trades_run", "backtest_trades", ["backtest_run_id"])


def downgrade() -> None:
    op.drop_index("ix_backtest_trades_run", table_name="backtest_trades")
    op.drop_table("backtest_trades")
    op.drop_table("backtest_equity_points")
    op.drop_index("ix_backtest_runs_version_created", table_name="backtest_runs")
    op.drop_table("backtest_runs")
    postgresql.ENUM(name="backtestrunstatus").drop(op.get_bind(), checkfirst=True)
