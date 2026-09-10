"""universe_scans + universe_scan_results - one validated StrategyVersion
run across many symbols and ranked (Phase 60)

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-10

**A scan is a BATCH of ordinary backtests, not a new kind of execution.**
The orchestrator that fills these tables
(`apps/api/app/backtesting/universe_scan.py`) calls
`apps/api/app/backtesting/engine_v2.py::run_strategy_backtest()` unchanged,
once per symbol, and each call leaves behind a real, fully persisted
`backtest_runs` row (migration 0018) with its own equity curve and trades.
`universe_scan_results.backtest_run_id` points at exactly that row, the same
structure `walk_forward_windows` (migration 0019) has to the windows it
replays - and deliberately NOT the structure `robustness_perturbations`
(migration 0021) has. That table stores its own metrics with no
`backtest_runs` FK because a perturbation replays a definition no version
contains; a scanned symbol replays the version's OWN definition, unaltered,
so a `backtest_runs` row filed under that `strategy_version_id` is an
accurate record of what actually ran. Same test applied, opposite answer,
because the inputs genuinely differ.

Every symbol is scanned over the SAME window at the SAME `starting_cash`
under the SAME risk and portfolio limits. Symbols do not share or compound a
balance - these are parallel measurements meant to be compared with each
other, the same reasoning migration 0019 gives for walk-forward's windows.
A ranking whose rows covered different periods or different capital bases
would not be a ranking.

**`universe_scans.strategy_version_id` is `ON DELETE RESTRICT`**, the
reasoning migrations 0017/0018/0019/0021 already gave: a persisted result's
exact input must never be able to disappear. A table of twenty ranked
symbols means nothing without the definition they were ranked under.
**`universe_scan_results.backtest_run_id` is also `ON DELETE RESTRICT`** -
the row it reports on must not vanish either, matching
`walk_forward_windows.backtest_run_id` exactly. **
`universe_scan_results.universe_scan_id` is `ON DELETE CASCADE`**: a
per-symbol result has no meaning apart from the scan that requested it, the
same relationship `walk_forward_windows` has to `walk_forward_runs` and
`backtest_equity_points` has to `backtest_runs`.

One new enum type, `universescanstatus` (`running`, `succeeded`, `failed`) -
no `pending`, matching `backtestrunstatus` / `walkforwardrunstatus` /
`robustnessrunstatus`: the orchestrator creates the row already `running` and
resolves it to a terminal status inside the same request, so nothing ever
observes a row any earlier. `universe_scan_results.status` is deliberately a
plain `String(16)` rather than a second enum type, and so is
`universe_scans.scan_mode` - both are display/provenance values never
branched on outside their own row, the precedent `backtest_trades.side` set.

**`universe_scans.requested_symbols` is a `postgresql.ARRAY(String(32))`,
the first array column in this schema since `roles.permissions`** (migration
0004) - a short, flat list of strings read as a whole, never joined against
and never filtered by, which a child table would model at the cost of a join
that buys nothing. It holds the EXACT list the caller asked for, normalized
(trimmed, upper-cased, de-duplicated): provenance, not a result. What was
actually scanned is what `num_symbols` counts and what the
`universe_scan_results` rows enumerate, and keeping the request verbatim
beside them is what makes any difference between the two visible. It is
NULL - not `{}` - when `scan_mode = 'all_ingested'`, because the caller
named no symbols at all and an empty array would read as "asked for none".

Every aggregate column on both tables is NULLABLE, for the same
no-fabrication reason every `backtest_runs`, `walk_forward_runs` and
`robustness_runs` metric column is (docs/TRADING_SAFETY.md). A `FAILED`
scan computed none of them and `NULL` says so where `0` would be a lie about
a number that was never produced. One level down, a `failed` per-symbol
result - most often a symbol with no ingested bars over this window, which
is an ordinary outcome of scanning a list someone typed - has no return, no
drawdown and no trade count, contributes NOTHING to the parent scan's
counts rather than contributing a zero, and still gets its own row with its
own `error_detail` so the gap stays visible instead of being quietly dropped
from the universe.

`num_symbols` counts every symbol attempted; `num_succeeded` counts those
whose own backtest reached `succeeded`; `num_qualified` counts the succeeded
ones with `total_return_pct > 0` - that is, "profitable over this exact
window", and nothing more. That is a deliberately simple first-pass filter,
stated plainly so its meaning is fully known and adjustable later, in the
same spirit as D075's scoring thresholds and D076's leaderboard tiers. It
says nothing about risk-adjusted return, drawdown, trade count or
statistical significance, and it is not a recommendation to trade anything.

There is deliberately no `rank` column on `universe_scan_results`. Rank is
computed on read, over the succeeded rows by `total_return_pct` descending,
exactly as D076's leaderboard ranks on read: a stored rank would be a second
copy of an ordering the data already fully determines, and any later change
to how ranking is defined would silently disagree with every row written
under the old rule.

`ix_universe_scans_version_created` supports the only listing query, "this
version's scans, newest first". `ix_universe_scan_results_scan` supports
fetching one scan's full per-symbol breakdown for its detail response.
`uq_universe_scan_result_scan_symbol` makes "one row per symbol per scan" a
schema guarantee rather than an orchestrator convention - the same shape
`uq_walk_forward_window_run_index` gives windows.

Downgrade drops `universe_scan_results` first (it references
`universe_scans`), then `universe_scans`, then the enum type. Nothing else
in the schema references either table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    universe_scan_status_enum = postgresql.ENUM(
        "running",
        "succeeded",
        "failed",
        name="universescanstatus",
        create_type=False,
    )
    universe_scan_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "universe_scans",
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
        sa.Column("bar_interval", sa.String(length=8), nullable=False, server_default="1d"),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("starting_cash", sa.Numeric(20, 6), nullable=False),
        sa.Column("scan_mode", sa.String(length=16), nullable=False),
        sa.Column("requested_symbols", postgresql.ARRAY(sa.String(length=32)), nullable=True),
        sa.Column("status", universe_scan_status_enum, nullable=False),
        sa.Column("num_symbols", sa.Integer(), nullable=True),
        sa.Column("num_succeeded", sa.Integer(), nullable=True),
        sa.Column("num_qualified", sa.Integer(), nullable=True),
        sa.Column("error_detail", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_universe_scans_version_created",
        "universe_scans",
        ["strategy_version_id", "created_at"],
    )

    op.create_table(
        "universe_scan_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "universe_scan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("universe_scans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column(
            "backtest_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("total_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("max_drawdown_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("win_rate_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("num_trades", sa.Integer(), nullable=True),
        sa.Column("error_detail", sa.String(length=500), nullable=True),
        sa.UniqueConstraint(
            "universe_scan_id", "symbol", name="uq_universe_scan_result_scan_symbol"
        ),
    )
    op.create_index(
        "ix_universe_scan_results_scan", "universe_scan_results", ["universe_scan_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_universe_scan_results_scan", table_name="universe_scan_results")
    op.drop_table("universe_scan_results")
    op.drop_index("ix_universe_scans_version_created", table_name="universe_scans")
    op.drop_table("universe_scans")
    postgresql.ENUM(name="universescanstatus").drop(op.get_bind(), checkfirst=True)
