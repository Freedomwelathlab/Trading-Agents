"""monte_carlo_runs - seeded bootstrap resampling of a completed
BacktestRun's trade returns into a distribution of simulated outcomes
(Phase 57)

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-09

Monte Carlo here answers a different question than a backtest or a
walk-forward run does. A backtest says "this is what happened, in this
exact order, over this exact window." Monte Carlo asks "if the SAME set of
trade outcomes had happened in a different order (or with some repeated
and some never occurring - real bootstrap resampling, with replacement),
how much would the result have varied by chance alone?" It is a statement
about the SEQUENCE risk already latent in the trades a real backtest
produced, not a claim about a different or better trading history.

**`backtest_run_id` is `ON DELETE RESTRICT`, and it is intentionally the
only foreign key this table needs** - a Monte Carlo run's entire input is
one existing `backtest_runs` row's trade list; there is no direct
dependency on `strategy_versions` because the backtest row already pins
that provenance. Restrict, not cascade, for the same reason every other
persisted-result FK in this schema is: the run this analysis is about must
not be able to disappear out from under it.

**Only aggregate percentile statistics are persisted, never every
simulated path.** With a default of 1,000+ simulations each holding a full
equity curve, storing every path would dwarf the space every other table
in this schema uses for comparatively little benefit - the percentile
bands, the probability of ruin, and the persisted `random_seed` are
exactly what the spec's own Monte Carlo section asks for (median CAGR, 5th/
95th percentile, expected and worst-case drawdown), and the seed alone is
sufficient to regenerate the full path distribution later if a deeper look
is ever needed, since the resampling algorithm is deterministic given a
seed and the same input trade list.

`random_seed` is `BigInteger`, not the `Integer` used elsewhere in this
schema - a seed is drawn from a wide random range specifically so it is
not a predictable or guessable value, and `Integer`'s 32-bit range is
narrower than is comfortable for that purpose.

One new enum type, `montecarlorunstatus` (`running`, `succeeded`,
`failed`) - the identical three-value shape `walkforwardrunstatus`
(migration 0019) uses and the identical reasoning: the orchestrator
creates the row already `running` and resolves it within the same
request, so `pending` describes a state nothing ever observes.

Every statistic column is NULLABLE - a `FAILED` run (too few trades in the
source backtest to resample meaningfully) computed none of them, and
`NULL`, not `0`, is what that means (docs/TRADING_SAFETY.md's
no-fabrication rule, the same one migration 0018's metric columns and
migration 0019's aggregate columns already follow).

`ix_monte_carlo_runs_backtest_created` supports the only listing query,
"this backtest run's Monte Carlo analyses, newest first" - a single
`BacktestRun` may reasonably be analyzed more than once, e.g. at different
`num_simulations` counts, so this is a one-to-many relationship, not
one-to-one.

Downgrade drops `monte_carlo_runs` then the enum type; nothing else in the
schema references this table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    monte_carlo_status_enum = postgresql.ENUM(
        "running",
        "succeeded",
        "failed",
        name="montecarlorunstatus",
        create_type=False,
    )
    monte_carlo_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "monte_carlo_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "backtest_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "requested_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("num_simulations", sa.Integer(), nullable=False),
        sa.Column("random_seed", sa.BigInteger(), nullable=False),
        sa.Column("status", monte_carlo_status_enum, nullable=False),
        sa.Column("num_trades_resampled", sa.Integer(), nullable=True),
        sa.Column("median_final_equity", sa.Numeric(20, 6), nullable=True),
        sa.Column("p5_final_equity", sa.Numeric(20, 6), nullable=True),
        sa.Column("p95_final_equity", sa.Numeric(20, 6), nullable=True),
        sa.Column("median_max_drawdown_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("p5_max_drawdown_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("p95_max_drawdown_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("probability_of_ruin_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("error_detail", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_monte_carlo_runs_backtest_created",
        "monte_carlo_runs",
        ["backtest_run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_monte_carlo_runs_backtest_created", table_name="monte_carlo_runs")
    op.drop_table("monte_carlo_runs")
    postgresql.ENUM(name="montecarlorunstatus").drop(op.get_bind(), checkfirst=True)
