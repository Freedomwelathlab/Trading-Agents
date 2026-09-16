"""backtest_runs records the cost assumptions it was executed under, and
what those costs came to (Phase 70, D088)

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-15

**What this adds.** Four nullable columns on `backtest_runs`:

    fee_bps         NUMERIC(10,4)   the per-side fee assumption, in bps
    slippage_bps    NUMERIC(10,4)   the slippage assumption, in bps
    total_fees      NUMERIC(20,6)   currency fees actually charged
    total_slippage  NUMERIC(20,6)   currency slippage actually modelled

No index, no constraint, no data migration.

**Why the assumptions are stored, not just the totals.** Before Phase 70
the v2 backtest engine modelled no transaction cost of any kind - not a
fee, not a spread, not slippage. Every number it has produced therefore
describes a frictionless market. Phase 70 changes that, and the moment it
does, two runs of the same definition over the same window can legitimately
disagree because the operator edited a setting in between. Storing the two
bps figures ON THE ROW makes each result self-describing: the equity curve
and the assumption that produced it travel together, and a result stays
interpretable long after the configuration that generated it has moved on.

**Why all four are NULLABLE, and why nothing is back-filled.** Runs created
before this migration genuinely executed with no cost model. Writing 0 into
their `fee_bps` would assert they were run with a zero-fee assumption -
which is a claim about a choice nobody made, and is exactly the kind of
plausible-looking fabricated figure docs/TRADING_SAFETY.md forbids. NULL
here means "this run predates cost modelling; its costs are unknown and its
returns are frictionless." That is a different and more honest statement
than zero, and any reader - or query - can tell the two apart.

The same reasoning is why there is no server_default: a default would
silently apply to those historical rows on the next write and erase the
distinction.

DOWNGRADE
---------
Drops the four columns. Lossy, and deliberately allowed to be: the columns
carry only the cost annotation of a run, never the run's identity, its
equity curve, or its trades, all of which survive untouched in
`backtest_runs`, `backtest_equity_points` and `backtest_trades`. A
downgraded database simply returns to not modelling costs - which is
precisely the state this migration exists to move away from, so nothing
downstream can be left pointing at a row that no longer explains itself.
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("backtest_runs", sa.Column("fee_bps", sa.Numeric(10, 4), nullable=True))
    op.add_column(
        "backtest_runs", sa.Column("slippage_bps", sa.Numeric(10, 4), nullable=True)
    )
    op.add_column("backtest_runs", sa.Column("total_fees", sa.Numeric(20, 6), nullable=True))
    op.add_column(
        "backtest_runs", sa.Column("total_slippage", sa.Numeric(20, 6), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("backtest_runs", "total_slippage")
    op.drop_column("backtest_runs", "total_fees")
    op.drop_column("backtest_runs", "slippage_bps")
    op.drop_column("backtest_runs", "fee_bps")
