"""strategy_deployment_runs gains SKIPPED_LIVE_RISK_HALT - the unattended
live runner stopping itself on a capital circuit breaker (Phase 69, D087)

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-11

**What this adds.** One new label on the `strategydeploymentrunstatus`
Postgres enum: `skipped_live_risk_halt`. No table, column, or index
changes: Phase 69 adds no new persisted entity. Its capital controls are
settings, and every figure the circuit breakers evaluate is derived per
cycle from rows that already exist (`orders`, `fills`,
`strategy_deployment_runs`) rather than from a remembered running total. A
breaker that trusts its own cached number is a breaker that keeps trading
after the number goes stale.

**Why a separate status from `skipped_live_trading_disabled`.** Migration
0025 added that label for Phase 64's refusal to place live orders at all.
Phase 69 keeps that refusal as the outcome whenever unattended live
execution is not armed - which is every default checkout - but it now
needs to describe a second, opposite situation: the robot WAS armed,
running correctly on real money, and halted itself because it lost more in
one day than `STRATEGY_LIVE_MAX_DAILY_LOSS_PCT` allows, or lost more in
total than `STRATEGY_LIVE_MAX_TOTAL_LOSS_PCT` allows, or would have
breached the total-capital or open-position ceiling.

Collapsing the two into one label would make "the robot never started" and
"the robot stopped itself after losing money" indistinguishable in the
audit trail. They call for opposite operator responses, and the second one
is urgent.

**Why the label is added outside the migration's own transaction.**
Postgres refuses to use a value added by `ALTER TYPE ... ADD VALUE` inside
the transaction that added it. `op.execute("COMMIT")` before the
`ADD VALUE` follows migrations 0014 and 0025 exactly, so the label is
usable the moment this migration returns.

DOWNGRADE
---------
Postgres has no `ALTER TYPE ... DROP VALUE`, so the downgrade rebuilds the
type - and refuses first if any row actually uses the new label. A halt row
records that real money stopped being traded at a specific moment for a
specific reason; rewriting it to make a downgrade succeed would destroy
precisely the audit record this project's anti-fabrication rule protects.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("COMMIT")
    op.execute(
        "ALTER TYPE strategydeploymentrunstatus ADD VALUE IF NOT EXISTS "
        "'skipped_live_risk_halt'"
    )


def downgrade() -> None:
    in_use = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM strategy_deployment_runs "
            "WHERE status = 'skipped_live_risk_halt'"
        )
    ).scalar_one()
    if in_use:
        raise RuntimeError(
            f"{in_use} strategy_deployment_runs row(s) use 'skipped_live_risk_halt'. "
            "Each one records a live capital circuit breaker firing on real money. "
            "Downgrading would require rewriting or deleting that outcome, which this "
            "migration exists to prevent (docs/DECISIONS.md D087). Resolve or archive "
            "those rows first."
        )

    op.execute("ALTER TYPE strategydeploymentrunstatus RENAME TO strategydeploymentrunstatus_old")
    op.execute(
        "CREATE TYPE strategydeploymentrunstatus AS ENUM ("
        "'succeeded', 'failed', 'skipped_not_active', 'skipped_emergency_stop', "
        "'skipped_market_closed', 'skipped_lock_held', 'skipped_live_trading_disabled'"
        ")"
    )
    op.execute(
        "ALTER TABLE strategy_deployment_runs ALTER COLUMN status TYPE "
        "strategydeploymentrunstatus USING status::text::strategydeploymentrunstatus"
    )
    op.execute("DROP TYPE strategydeploymentrunstatus_old")
