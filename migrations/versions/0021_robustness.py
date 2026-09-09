"""robustness_runs + robustness_perturbations - parameter-sensitivity
testing of a StrategyVersion (Phase 58)

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-09

**This is sensitivity analysis, NOT optimization and NOT a search.** The
orchestrator that fills these tables replays one window twice over: once
with the definition exactly as its author wrote it (the baseline), then
once more per numeric parameter nudged up and down by `magnitude_pct`.
Nothing looks for a BETTER parameter set, ranks the perturbed variants as
candidates, or writes any of them back to a `strategy_versions` row - a
StrategyDefinition's rules are fixed by whoever authored them (D071), and
each perturbed definition exists only in memory for the length of the
replay that measures it. The question these rows answer is about the
ORIGINAL definition: does its result survive small changes to its own
numbers, or was it balanced on one exact parameter set that happened to
suit this history? Calling this "optimization" would claim a capability
this platform deliberately does not have, the same distinction migration
0019 draws for walk-forward.

**A perturbation is NOT persisted as a `backtest_runs` row**, which is the
one structural difference from `walk_forward_windows` (migration 0019).
That table points each window at a real, ordinary `BacktestRun` because a
window replays the version's own definition, unchanged. A perturbation
replays a definition NO version contains, and a `backtest_runs` row is a
record of a version's definition - filing one under a `strategy_version_id`
whose definition says something else would misattribute the result. So the
orchestrator reaches for `apps/api/app/backtesting/engine_v2.py`'s pure,
no-persistence helpers (`_load_warmup_and_window`, `_replay_window`)
instead, and the two metric columns on `robustness_perturbations` are
everything a variant's replay leaves behind. Reusing those helpers rather
than writing a second replay path matters for the same reason D072 reused
`engine.py`'s `_attempt_trade`: two independent implementations can drift
apart, one cannot.

Every perturbation runs over the SAME window, at the SAME `starting_cash`,
under the SAME risk and portfolio limits as the baseline. The one and only
difference between a perturbed replay and the baseline replay is the single
parameter named in `parameter_path` - anything else varying would confound
exactly the comparison the run exists to make.

**`robustness_runs.strategy_version_id` is `ON DELETE RESTRICT`**, the same
reasoning migrations 0017/0018/0019 already gave: a persisted result's exact
input must never be able to disappear. Which parameters were perturbed, and
from what values, mean nothing without the definition they were perturbed
FROM. **`robustness_perturbations.robustness_run_id` is `ON DELETE
CASCADE`**: a perturbation result has no meaning apart from the parent run
that requested it, the same relationship `walk_forward_windows` has to
`walk_forward_runs` and `backtest_equity_points` has to `backtest_runs`.

One new enum type, `robustnessrunstatus` (`running`, `succeeded`, `failed`)
- no `pending`, matching `backtestrunstatus` / `walkforwardrunstatus`: the
orchestrator creates the row already `running` and resolves it to a terminal
status within the same request, so nothing ever observes a row any earlier.
`robustness_perturbations.status` is deliberately a plain `String(16)` and
not a second enum type: that value is display-only and never branched on
outside its own row, the same precedent `backtest_trades.side` set.

Every metric column on both tables is NULLABLE, for the same
no-fabrication reason every `backtest_runs` and `walk_forward_runs` metric
column is. A `FAILED` run - a definition with no numeric parameters to
perturb at all (e.g. `all_in` sizing and no indicators), or a baseline
replay that itself could not run - computed none of them, and `NULL` says
so where `0` would be a lie about a number that was never produced. The
same holds one level down: a `failed` perturbation (most often a much larger
perturbed indicator period needing more warmup history than the bar store
holds) has no return and no drawdown, contributes NOTHING to the parent
run's aggregates rather than contributing a zero, and still gets its own row
with its own `error_detail` so the gap is visible rather than silently
smoothed over. `num_perturbations` counts every perturbation attempted;
`num_succeeded_perturbations` is always `<=` it, and is what says how many
numbers went into the aggregates.

`max_return_deviation_pct` - the largest absolute gap between a succeeded
perturbation's return and the baseline's - is deliberately one simple,
honest number rather than a composite "robustness score". This phase does
not attempt to define a single weighted figure across return, drawdown and
whatever else: any such weighting would encode a risk preference nobody has
stated, and a lone 0-100 score invites being read as an authoritative
verdict on a strategy. A future phase can build a scoring model on top of
these raw columns if one is ever wanted; it could not recover the raw
numbers from a score.

`ix_robustness_runs_version_created` supports the only listing query, "this
version's robustness runs, newest first". `ix_robustness_perturbations_run`
supports fetching one run's full perturbation breakdown for its detail
response. There is deliberately no unique constraint on
`(robustness_run_id, parameter_path, direction)`: a generator is free to
emit several variants of one parameter in a future phase, and a uniqueness
rule invented here would be a schema-level guess at a policy that lives in
the generator.

Downgrade drops `robustness_perturbations` first (it references
`robustness_runs`), then `robustness_runs`, then the enum type. Nothing else
in the schema references either table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    robustness_status_enum = postgresql.ENUM(
        "running",
        "succeeded",
        "failed",
        name="robustnessrunstatus",
        create_type=False,
    )
    robustness_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "robustness_runs",
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
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("starting_cash", sa.Numeric(20, 6), nullable=False),
        sa.Column("magnitude_pct", sa.Numeric(10, 4), nullable=False),
        sa.Column("status", robustness_status_enum, nullable=False),
        sa.Column("baseline_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("baseline_max_drawdown_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("num_perturbations", sa.Integer(), nullable=True),
        sa.Column("num_succeeded_perturbations", sa.Integer(), nullable=True),
        sa.Column("mean_perturbed_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("stddev_perturbed_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("max_return_deviation_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("error_detail", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_robustness_runs_version_created",
        "robustness_runs",
        ["strategy_version_id", "created_at"],
    )

    op.create_table(
        "robustness_perturbations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "robustness_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("robustness_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("parameter_path", sa.String(length=128), nullable=False),
        sa.Column("direction", sa.String(length=4), nullable=False),
        sa.Column("original_value", sa.Numeric(20, 6), nullable=False),
        sa.Column("perturbed_value", sa.Numeric(20, 6), nullable=False),
        sa.Column("clamped", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("total_return_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("max_drawdown_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("error_detail", sa.String(length=500), nullable=True),
    )
    op.create_index(
        "ix_robustness_perturbations_run", "robustness_perturbations", ["robustness_run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_robustness_perturbations_run", table_name="robustness_perturbations")
    op.drop_table("robustness_perturbations")
    op.drop_index("ix_robustness_runs_version_created", table_name="robustness_runs")
    op.drop_table("robustness_runs")
    postgresql.ENUM(name="robustnessrunstatus").drop(op.get_bind(), checkfirst=True)
