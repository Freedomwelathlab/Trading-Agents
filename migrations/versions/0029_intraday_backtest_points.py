"""backtest equity points and trades become intraday-capable (Phase 71, D089)

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-15

**The bug this fixes is a hard failure, not a cosmetic one.** Phase 55
built the backtest tables when `1d` was the only interval anything
ingested, so it keyed the equity curve by CALENDAR DAY:

    UNIQUE (backtest_run_id, date)

An hourly backtest produces 24 equity points per calendar day, so the very
first intraday run dies on

    duplicate key value violates unique constraint
    "uq_backtest_equity_point_run_date"

after the replay has already completed. Phase 70 widened `bar_interval` to
the intraday vocabulary and Phase 71 ingested 4,445 real hourly BTC/USD
bars; this is the constraint that stood between those bars and a result.

WHAT CHANGES
------------
`backtest_equity_points`
  + `ts TIMESTAMPTZ NOT NULL`   the point's real bar timestamp
  - UNIQUE (backtest_run_id, date)
  + UNIQUE (backtest_run_id, ts)

`backtest_trades`
  + `entry_ts TIMESTAMPTZ NULL`
  + `exit_ts  TIMESTAMPTZ NULL`

**`date` is KEPT, not replaced.** It still answers "which day did this
happen on", which the monthly-returns heatmap groups by and which reads
naturally in the trade ledger. Dropping it would force every consumer to
re-derive a day from a timestamp, and re-deriving a calendar day from an
instant is exactly where timezone bugs live. The two now coexist with `ts`
authoritative and `date` its UTC day.

**Existing rows are back-filled from `date`, at midnight UTC.** That is
truthful for them precisely because they are all daily-bar runs: a daily
bar's point genuinely belongs to its day, and no finer instant was ever
known. It would NOT be truthful for an intraday row, which is why the
column becomes NOT NULL only after the backfill and why nothing writes a
midnight default from here on - `engine_v2` writes each bar's own vendor
timestamp.

**The trade columns are NULLABLE and are not back-filled.** A historical
trade row knows only its date; inventing 00:00:00 for it would assert an
execution time that was never recorded, which is the fabricated-figure
problem `docs/TRADING_SAFETY.md` forbids. NULL there means "this run
predates intraday trade timestamps", readably distinct from a real
midnight fill.

DOWNGRADE
---------
Restores the day-keyed unique constraint and drops the three columns. It
will FAIL, deliberately, if any run holds two points on one calendar day -
that is an intraday run, and silently discarding 23 of every 24 points to
fit the old shape would corrupt a real result rather than reverse a
migration. Delete intraday runs first if a downgrade is genuinely wanted.
"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backtest_equity_points", sa.Column("ts", sa.DateTime(timezone=True), nullable=True)
    )
    # Truthful for every existing row: they are all daily-bar runs, whose
    # points belong to the day and to no finer instant.
    op.execute(
        "UPDATE backtest_equity_points "
        "SET ts = (date::timestamp AT TIME ZONE 'UTC') WHERE ts IS NULL"
    )
    op.alter_column("backtest_equity_points", "ts", nullable=False)

    op.drop_constraint(
        "uq_backtest_equity_point_run_date", "backtest_equity_points", type_="unique"
    )
    op.create_unique_constraint(
        "uq_backtest_equity_point_run_ts", "backtest_equity_points", ["backtest_run_id", "ts"]
    )

    op.add_column(
        "backtest_trades", sa.Column("entry_ts", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "backtest_trades", sa.Column("exit_ts", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    duplicates = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM ("
            "  SELECT backtest_run_id, date FROM backtest_equity_points"
            "  GROUP BY backtest_run_id, date HAVING count(*) > 1"
            ") d"
        )
    ).scalar_one()
    if duplicates:
        raise RuntimeError(
            f"{duplicates} (run, date) pair(s) hold more than one equity point, which means "
            "at least one INTRADAY backtest exists. Restoring the day-keyed unique "
            "constraint would require discarding real equity points from a real result. "
            "Delete the intraday runs first if this downgrade is genuinely intended."
        )

    op.drop_column("backtest_trades", "exit_ts")
    op.drop_column("backtest_trades", "entry_ts")
    op.drop_constraint(
        "uq_backtest_equity_point_run_ts", "backtest_equity_points", type_="unique"
    )
    op.create_unique_constraint(
        "uq_backtest_equity_point_run_date",
        "backtest_equity_points",
        ["backtest_run_id", "date"],
    )
    op.drop_column("backtest_equity_points", "ts")
