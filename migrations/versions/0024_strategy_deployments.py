"""strategy_deployments + strategy_deployment_runs - a validated
StrategyVersion put on a scheduled paper-trading runner, behind a mandatory
human-approval gate (Phase 63)

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-10

**This is the first thing in the codebase that places an order with no HTTP
request and no human in the loop for that specific order.** Every earlier
order-creation path - `POST /brokers/{id}/trades`, the agent-trade endpoint,
a backtest replay - either has an authenticated caller for that trade or is
a simulation that touches no persisted broker. A deployment runner
re-evaluates a strategy on an interval and, when the strategy's own rules
say so, calls the same `oms.service.submit_trade()` path everything else
does, against a real (paper) `broker_accounts` row. The schema is built
around making that safe and auditable, not around making it easy.

**The mandatory human-approval gate is a column, not a convention.** A
`strategy_deployments` row is created in `PENDING_APPROVAL` and the runner
will not look at it. A separate, explicit approval action (its own API
call, its own actor, recorded in `approved_by_user_id` / `approved_at`)
moves it to `ACTIVE`. There is no code path that creates a row already
`ACTIVE`; spec §25/§52 and docs/TRADING_SAFETY.md require a person to
deliberately turn a strategy on before it trades, even in paper mode, and a
state machine that cannot express "approved" as distinct from "created"
could not enforce that.

**`mode` is a string and only `paper` is accepted.** Phase 64 adds `live`
behind the existing `TRADING_MODE=live` / `LIVE_TRADING_ENABLED` /
live-credential triple gate; until then the API rejects any value but
`paper` (a 422, before a row exists). The column exists now so that
addition needs no migration and so a reader can see the distinction was
always intended.

**`strategy_version_id` is `ON DELETE RESTRICT`** - the exact rules a live
(paper) position was opened under must never be able to vanish, the same
reasoning migrations 0018-0023 gave for every persisted result.
`broker_id` is likewise `RESTRICT`: a deployment names the account it
trades, and that account's history is the record of what it did.
`requested_by_user_id` / `approved_by_user_id` are `SET NULL` - who asked
and who approved outlives the accounts, matching every sibling.

**`strategy_deployment_runs` is an append-only audit of every cycle**, the
same posture as `signal_evaluations` and the reconciler's outcomes: a run
that did nothing, was skipped because the deployment was paused, or failed
mid-cycle each writes a row saying so. A missing row must never be the only
evidence that a cycle happened. `ON DELETE CASCADE` to its deployment -
the runs have no meaning without it, unlike a result whose inputs must be
pinned.

`strategydeploymentstatus`: `pending_approval` -> `active` <-> `paused`,
and any non-terminal state -> `stopped` (terminal). `strategydeploymentrunstatus`
records why a cycle did or did not act: `succeeded` / `failed` /
`skipped_not_active` / `skipped_emergency_stop` / `skipped_market_closed` /
`skipped_lock_held`.

Two existing tables gain one nullable `deployment_run_id` FK each, both
`ON DELETE SET NULL`:
- `orders` - so an order placed by a runner is attributable to the exact
  cycle that placed it (the `Order` model docstring already anticipated a
  non-human order-creation path).
- `signal_evaluations` - the runner persists a real Phase-61
  `SignalEvaluation` per symbol per cycle (reused, not reimplemented); the
  FK is what separates a deployment's signal trail from an ad-hoc
  `POST .../signals` call, whose column stays NULL.

Downgrade drops the two FK columns, then the two tables, then the two enum
types. Nothing else references them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    deployment_status = postgresql.ENUM(
        "pending_approval",
        "active",
        "paused",
        "stopped",
        name="strategydeploymentstatus",
        create_type=False,
    )
    deployment_status.create(op.get_bind(), checkfirst=True)

    run_status = postgresql.ENUM(
        "succeeded",
        "failed",
        "skipped_not_active",
        "skipped_emergency_stop",
        "skipped_market_closed",
        "skipped_lock_held",
        name="strategydeploymentrunstatus",
        create_type=False,
    )
    run_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "strategy_deployments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "strategy_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "broker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("brokers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("mode", sa.String(length=8), nullable=False, server_default="paper"),
        sa.Column("status", deployment_status, nullable=False),
        sa.Column(
            "symbols",
            postgresql.ARRAY(sa.String(length=32)),
            nullable=False,
        ),
        sa.Column("bar_interval", sa.String(length=8), nullable=False, server_default="1d"),
        sa.Column(
            "requested_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "approved_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_reason", sa.String(length=500), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_strategy_deployments_status", "strategy_deployments", ["status"]
    )
    op.create_index(
        "ix_strategy_deployments_version",
        "strategy_deployments",
        ["strategy_version_id", "created_at"],
    )

    op.create_table(
        "strategy_deployment_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "deployment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_deployments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", run_status, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("symbols_evaluated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signals_actionable", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("orders_submitted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("orders_filled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_strategy_deployment_runs_deployment",
        "strategy_deployment_runs",
        ["deployment_id", "created_at"],
    )

    op.add_column(
        "orders",
        sa.Column(
            "deployment_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_deployment_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "signal_evaluations",
        sa.Column(
            "deployment_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_deployment_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("signal_evaluations", "deployment_run_id")
    op.drop_column("orders", "deployment_run_id")
    op.drop_index(
        "ix_strategy_deployment_runs_deployment", table_name="strategy_deployment_runs"
    )
    op.drop_table("strategy_deployment_runs")
    op.drop_index("ix_strategy_deployments_version", table_name="strategy_deployments")
    op.drop_index("ix_strategy_deployments_status", table_name="strategy_deployments")
    op.drop_table("strategy_deployments")
    postgresql.ENUM(name="strategydeploymentrunstatus").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="strategydeploymentstatus").drop(op.get_bind(), checkfirst=True)
