"""strategy improvement loop: iterative, hold-out-validated refinement
(Phase 72, D090)

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-15

**What this stores.** An improvement run is a recorded search: start from a
validated definition, repeatedly try one-parameter variants of the best
definition so far, and keep a variant only when it beats the incumbent on
data the search was not allowed to look at.

    strategy_improvement_runs    one search: its windows, its outcome
    strategy_improvement_steps   one candidate: what changed, what it did,
                                 whether it was accepted, and WHY

**Why the schema forces two windows.** A loop that proposes variants,
measures them, keeps the best and repeats is - stated plainly - a machine
for overfitting. Run it long enough against one window and it will find a
definition that fits that window's noise beautifully and predicts nothing.
`docs/DECISIONS.md` D073 already says the same about ranking.

So the search window and the judging window are SEPARATE COLUMNS, both
NOT NULL, and neither has a default. A caller cannot express "improve
against everything" even by accident, because there is no shape of this
table that means that. The train window is where candidates are generated
and scored; the validate window is never consulted until a candidate has
already won on train, and a candidate that wins on train while failing on
validate is recorded as REJECTED with that as its stated reason. Those
rejections are the most informative rows here: they are the search finding
noise and saying so.

**Every step keeps its backtest run ids.** `train_backtest_run_id` and
`validate_backtest_run_id` point at ordinary `backtest_runs` rows - the
same rows the history page lists and the chart draws. The loop invents no
private notion of a result: each step is auditable by opening the run it
came from, with its equity curve, its trade ledger and the exact fee and
slippage rates it was executed under.

**`accepted_reason` is free text and is always populated**, on acceptance
and rejection alike. A search whose steps only recorded numbers would
leave a reader to reverse-engineer why the loop stopped; the stated reason
is the difference between a record and a log.

DOWNGRADE
---------
Drops both tables. The `strategy_versions` a search created are NOT
touched: they are ordinary validated versions that may have been deployed
or backtested independently by then, and deleting real strategy definitions
as a side effect of reversing a bookkeeping table would be destructive well
beyond this migration's scope.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_STATUS_NAME = "strategyimprovementrunstatus"
_STATUS_VALUES = ("pending", "running", "succeeded", "failed")

_STATUS = postgresql.ENUM(*_STATUS_VALUES, name=_STATUS_NAME, create_type=False)
"""`create_type=False` so `create_table` does NOT try to emit the type a
second time. The type is created explicitly in `upgrade()` below with
`checkfirst=True`; without this flag SQLAlchemy also auto-creates it as a
side effect of the column, and the second CREATE TYPE fails with
`DuplicateObjectError` - after the first has already committed, leaving an
orphaned type and a half-applied migration."""


def upgrade() -> None:
    postgresql.ENUM(*_STATUS_VALUES, name=_STATUS_NAME).create(
        op.get_bind(), checkfirst=True
    )

    op.create_table(
        "strategy_improvement_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "strategy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "base_version_id",
            postgresql.UUID(as_uuid=True),
            # RESTRICT, matching backtest_runs: the definition a search
            # started from is what makes its result interpretable, and a
            # result whose input vanished is misleading rather than merely
            # incomplete.
            sa.ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "best_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("bar_interval", sa.String(8), nullable=False),
        sa.Column("train_start_date", sa.Date(), nullable=False),
        sa.Column("train_end_date", sa.Date(), nullable=False),
        sa.Column("validate_start_date", sa.Date(), nullable=False),
        sa.Column("validate_end_date", sa.Date(), nullable=False),
        sa.Column("starting_cash", sa.Numeric(20, 6), nullable=False),
        sa.Column("max_iterations", sa.Integer(), nullable=False),
        sa.Column("iterations_run", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidates_tested", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", _STATUS, nullable=False),
        # Every metric nullable: a FAILED search computed none of them, and
        # a 0 would be a fabricated figure.
        sa.Column("baseline_train_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("baseline_validate_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("best_train_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("best_validate_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("error_detail", sa.String(500), nullable=True),
        sa.Column(
            "requested_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_improvement_runs_strategy_created",
        "strategy_improvement_runs",
        ["strategy_id", "created_at"],
    )

    op.create_table(
        "strategy_improvement_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "improvement_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_improvement_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("candidate_index", sa.Integer(), nullable=False),
        sa.Column("parameter_path", sa.String(128), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("original_value", sa.Numeric(20, 6), nullable=True),
        sa.Column("perturbed_value", sa.Numeric(20, 6), nullable=True),
        sa.Column(
            "train_backtest_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "validate_backtest_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("train_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("validate_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("accepted_reason", sa.String(300), nullable=False),
        sa.Column(
            "created_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_improvement_steps_run_iteration",
        "strategy_improvement_steps",
        ["improvement_run_id", "iteration"],
    )


def downgrade() -> None:
    op.drop_index("ix_improvement_steps_run_iteration", table_name="strategy_improvement_steps")
    op.drop_table("strategy_improvement_steps")
    op.drop_index("ix_improvement_runs_strategy_created", table_name="strategy_improvement_runs")
    op.drop_table("strategy_improvement_runs")
    postgresql.ENUM(*_STATUS_VALUES, name=_STATUS_NAME).drop(
        op.get_bind(), checkfirst=True
    )
