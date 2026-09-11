"""strategy_deployment_runs gains SKIPPED_LIVE_TRADING_DISABLED - the
runner's unconditional refusal to place a live order (Phase 64, D082)

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-11

**What this adds.** One new label on the `strategydeploymentrunstatus`
Postgres enum: `skipped_live_trading_disabled`. Nothing else changes -
`strategy_deployments.mode` was already a plain `VARCHAR(8)` (migration
0024), so a `live` deployment needs no schema change to exist; only the
run-outcome vocabulary needs to grow to describe what the runner does with
one.

**Why this status exists at all.** Phase 63's runner places a (paper) order
with no HTTP request and no human confirming that specific trade - safe
there because the human approval already happened once, at deployment-approval
time, for a strategy whose worst case is fake money. Live trading has no
equivalent safe default: `docs/TRADING_SAFETY.md` requires an explicit,
in-the-moment human approval for every live trade, and a scheduled cycle
running unattended every `STRATEGY_RUNNER_INTERVAL_SECONDS` structurally
cannot supply that. So Phase 64 does not build a way for the runner to place
a live order at all - it builds the surrounding scaffolding (a `live`
deployment can be created and approved, behind its own
`STRATEGY_APPROVE_LIVE_DEPLOYMENT` permission) and makes the runner refuse
every cycle for it, unconditionally, writing this status and touching
nothing else - no market data read, no broker call, no risk engine. The
refusal does not consult `TRADING_MODE` / `LIVE_TRADING_ENABLED` at all,
so it cannot be switched off by an environment variable; a real
unattended-execution path is a future phase the user must separately design
and explicitly approve.

**Why a migration, not just a Python-side check.** `strategydeploymentrunstatus`
is a native Postgres enum (`ARRAY`/`Enum` via SQLAlchemy, not a string
column), so every value the ORM can write must exist in the type first.

**Why the label is added outside the migration's own transaction.** Postgres
refuses to use a value added by `ALTER TYPE ... ADD VALUE` inside the same
transaction that added it. This migration does not need the new label for
anything else within itself (unlike migration 0014's partial index), but
`op.execute("COMMIT")` before the `ADD VALUE` is kept anyway, matching that
migration's precedent exactly, so the label is guaranteed usable the moment
this migration returns rather than depending on Alembic's own transaction
boundaries.

DOWNGRADE
---------
Postgres has no `ALTER TYPE ... DROP VALUE`. The downgrade rebuilds
`strategydeploymentrunstatus` with the original six labels, first refusing
if any `strategy_deployment_runs` row actually uses the new one - silently
rewriting a real, persisted run outcome to make a downgrade succeed would
destroy exactly the audit record this project's anti-fabrication rule
protects.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("COMMIT")
    op.execute(
        "ALTER TYPE strategydeploymentrunstatus ADD VALUE IF NOT EXISTS "
        "'skipped_live_trading_disabled'"
    )


def downgrade() -> None:
    in_use = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM strategy_deployment_runs "
            "WHERE status = 'skipped_live_trading_disabled'"
        )
    ).scalar_one()
    if in_use:
        raise RuntimeError(
            f"{in_use} strategy_deployment_runs row(s) use "
            "'skipped_live_trading_disabled'. Downgrading would require rewriting or "
            "deleting that real, persisted run outcome, which migration 0025 exists to "
            "prevent (docs/DECISIONS.md D082). Resolve or archive those rows first."
        )

    op.execute("ALTER TYPE strategydeploymentrunstatus RENAME TO strategydeploymentrunstatus_old")
    op.execute(
        "CREATE TYPE strategydeploymentrunstatus AS ENUM ("
        "'succeeded', 'failed', 'skipped_not_active', 'skipped_emergency_stop', "
        "'skipped_market_closed', 'skipped_lock_held'"
        ")"
    )
    op.execute(
        "ALTER TABLE strategy_deployment_runs ALTER COLUMN status TYPE "
        "strategydeploymentrunstatus USING status::text::strategydeploymentrunstatus"
    )
    op.execute("DROP TYPE strategydeploymentrunstatus_old")
