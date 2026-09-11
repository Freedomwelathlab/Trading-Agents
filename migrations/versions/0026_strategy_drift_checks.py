"""strategy_drift_checks - an append-only audit row per runner cycle
recording whether a deployment's real performance has drifted from its own
real backtest (Phase 66, D084)

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-11

**What this adds.** One new table, `strategy_drift_checks`, and one new
native Postgres enum, `driftcheckstatus` (`no_drift` / `drift_detected` /
`insufficient_data`). Nothing existing changes shape.

**Why this table is `strategy_deployment_runs`' sibling, not its column.**
Phase 65 (D083) answered "is this deployment doing what the backtest said it
would" as a pure, unpersisted read (`build_deployment_monitoring`). Phase 66
asks the same question automatically, once per successful runner cycle, and
needs to remember the answer: an operator auditing what the system decided
needs the verdict for cycle N even after cycle N+1 has moved on. That is an
append-only audit trail, the same shape `strategy_deployment_runs` already
is for "what did this cycle do" - so this table follows it column-for-column
where the shapes coincide (`deployment_id` FK, `created_at` server-default,
an `(deployment_id, created_at)` index) rather than inventing a different
convention for a sibling concept.

**`deployment_id` is `ON DELETE CASCADE`**, matching
`strategy_deployment_runs.deployment_id` exactly - not `RESTRICT` like
`strategy_version_id`/`broker_id` on `strategy_deployments` itself. A drift
check is diagnostic history scoped to ITS deployment's own lifecycle, with
no meaning once the deployment itself is gone, unlike a `BacktestRun` or a
`StrategyVersion` whose exact inputs must stay pinned for as long as
anything might reference them.

**Numeric precision matches `backtest_runs.win_rate_pct`** -
`NUMERIC(10, 4)`, nullable, on all three of `actual_win_rate_pct` /
`expected_win_rate_pct` / `win_rate_deviation_pct`: a drift check's win-rate
numbers must round exactly the way the backtest's own win rate does, or a
reader comparing this row to the `BacktestRun` it references would see two
different numbers claiming to be the same rate. All three are nullable
together - `status = 'insufficient_data'` leaves all three `NULL`, never a
guessed number computed from too small a sample (docs/TRADING_SAFETY.md's
"no fabrication" rule, applied to a missing statistic instead of missing
market data, the same posture `deployments/monitoring.py` already takes for
`win_rate_pct`/`avg_return_pct` at zero round trips).

**`action_taken` is a plain `VARCHAR(32)`, not a Postgres enum.** Unlike
`status` (a closed, exhaustive vocabulary this codebase enumerates and
tests against everywhere), `action_taken` is closer to `error_detail` on
`strategy_deployment_runs` - a small set of literal strings
(`"none"`/`"observed_only"`/`"paused"`) written from Python, with no need
for the schema itself to enforce the enumeration. A future action (e.g. a
notification sent instead of a pause) would need no migration to add.

DOWNGRADE
---------
This is a brand-new table, not a growing enum on an existing one. There is
no way for a downgrade here to require choosing between rewriting live
history and refusing (contrast migration 0025's `ADD VALUE`/`DROP VALUE`
problem on `strategydeploymentrunstatus`, an enum other tables' rows already
reference): dropping `strategy_drift_checks` entirely also removes every row
that could ever be "in use," and nothing else in the schema references this
table, so there is nothing left behind to protect. The downgrade drops the
table, then the enum type - the structurally simplest choice, and correct
here specifically because this table has no downstream readers of its
labels to break.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    drift_status = postgresql.ENUM(
        "no_drift",
        "drift_detected",
        "insufficient_data",
        name="driftcheckstatus",
        create_type=False,
    )
    drift_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "strategy_drift_checks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "deployment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_deployments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", drift_status, nullable=False),
        sa.Column("actual_win_rate_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("expected_win_rate_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("win_rate_deviation_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("num_round_trips", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("action_taken", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_strategy_drift_checks_deployment",
        "strategy_drift_checks",
        ["deployment_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_strategy_drift_checks_deployment", table_name="strategy_drift_checks")
    op.drop_table("strategy_drift_checks")
    postgresql.ENUM(name="driftcheckstatus").drop(op.get_bind(), checkfirst=True)
