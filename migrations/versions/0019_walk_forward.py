"""walk_forward_runs + walk_forward_windows - sequential out-of-sample
consistency testing over a StrategyVersion (Phase 57)

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-09

**This is deliberately NOT "walk-forward optimization."** The classic
technique re-fits a strategy's parameters on each in-sample window before
testing it on the following out-of-sample window; this platform has no
parameter-fitting step at all (a StrategyDefinition's rules are fixed by
whoever authored it - see D071), so there is nothing to re-fit. What this
table records instead is honestly narrower and just as useful: the SAME
fixed, validated definition replayed across several sequential,
non-overlapping historical windows, to see whether its performance is
consistent across periods or an artifact of the one window a single
backtest happened to cover. Calling this "walk-forward optimization" would
claim a capability that does not exist; "walk-forward validation" or
"rolling consistency testing" is what actually happens, and
`apps/api/app/backtesting/walk_forward.py`'s docstring makes the same
distinction.

**Each window is a real, ordinary `BacktestRun`, not a second execution
path.** `walk_forward_windows.backtest_run_id` points at a row in the
table migration 0018 created; the orchestrator that builds those rows
calls `apps/api/app/backtesting/engine_v2.py::run_strategy_backtest()`
unchanged, once per window. Reusing it here matters for the same reason
D072 reused `engine.py`'s `_attempt_trade` for engine_v2 itself: two
independent replay implementations could quietly drift apart, one
implementation cannot.

Every window starts fresh at the SAME `starting_cash`, not a running
balance carried from the previous window. A walk-forward consistency test
asks "does this strategy perform similarly across different periods," not
"what would compounding through all of them produce" - a single ordinary
backtest over the full range already answers the second question, and
mixing the two would make "windows behaved inconsistently" and "an early
loss shrank the capital available to a later window" indistinguishable.

**`walk_forward_runs.strategy_version_id` is `ON DELETE RESTRICT`**, the
same reasoning migration 0017/0018 already gave for `backtest_runs`: a
persisted result's exact input must never be able to disappear.
**`walk_forward_windows.backtest_run_id` is also `ON DELETE RESTRICT`**
for the identical reason, one level down - a window's own recorded result
must not vanish either. `walk_forward_windows.walk_forward_run_id` is
`ON DELETE CASCADE`: a window has no meaning apart from the parent run
that requested it, the same relationship `backtest_equity_points` has to
`backtest_runs`.

One new enum type, `walkforwardrunstatus` (`running`, `succeeded`,
`failed`) - no `pending`, matching `backtestrunstatus`'s reasoning in
migration 0018: the orchestrator creates the row already `running` and
resolves it to a terminal status within the same request, so nothing ever
observes a row any earlier than that.

Every aggregate metric column on `walk_forward_runs` is NULLABLE, for the
same no-fabrication reason every `backtest_runs` metric column is: a
`FAILED` walk-forward run (e.g. fewer than two complete windows fit in the
requested range - "consistency" is not a claim that can be made about one
window) computed none of them, and `NULL` says so; a `0` would be a lie
about a number that was never computed. `num_windows` counts every window
attempted (including any that individually failed, e.g. one window with
an unrelated bar-store gap); `num_succeeded_windows` and
`num_profitable_windows` are always `<= num_windows`, and the aggregate
statistics (`mean_return_pct` etc.) are computed only over the succeeded
subset - a failed window contributes nothing to be averaged, not a zero.

`ix_walk_forward_runs_version_created` supports the only listing query,
"this version's walk-forward runs, newest first". `ix_walk_forward_windows_run`
supports fetching one run's full window breakdown for its detail response.
`uq_walk_forward_window_run_index` guarantees a run's windows are
addressable by a stable, gap-free sequence number and can never be written
twice.

Downgrade drops `walk_forward_windows` first (it references both
`walk_forward_runs` and `backtest_runs`), then `walk_forward_runs`, then
the enum type. Nothing else in the schema references either table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    walk_forward_status_enum = postgresql.ENUM(
        "running",
        "succeeded",
        "failed",
        name="walkforwardrunstatus",
        create_type=False,
    )
    walk_forward_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "walk_forward_runs",
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
        sa.Column("overall_start_date", sa.Date(), nullable=False),
        sa.Column("overall_end_date", sa.Date(), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("starting_cash", sa.Numeric(20, 6), nullable=False),
        sa.Column("status", walk_forward_status_enum, nullable=False),
        sa.Column("num_windows", sa.Integer(), nullable=True),
        sa.Column("num_succeeded_windows", sa.Integer(), nullable=True),
        sa.Column("num_profitable_windows", sa.Integer(), nullable=True),
        sa.Column("mean_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("stddev_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("best_window_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("worst_window_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("error_detail", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_walk_forward_runs_version_created",
        "walk_forward_runs",
        ["strategy_version_id", "created_at"],
    )

    op.create_table(
        "walk_forward_windows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "walk_forward_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("walk_forward_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("window_index", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column(
            "backtest_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "walk_forward_run_id", "window_index", name="uq_walk_forward_window_run_index"
        ),
    )
    op.create_index(
        "ix_walk_forward_windows_run", "walk_forward_windows", ["walk_forward_run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_walk_forward_windows_run", table_name="walk_forward_windows")
    op.drop_table("walk_forward_windows")
    op.drop_index("ix_walk_forward_runs_version_created", table_name="walk_forward_runs")
    op.drop_table("walk_forward_runs")
    postgresql.ENUM(name="walkforwardrunstatus").drop(op.get_bind(), checkfirst=True)
