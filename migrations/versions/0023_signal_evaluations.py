"""signal_evaluations - what a validated StrategyVersion currently says to do
for one symbol, off the latest ingested bars, with the reasoning kept
(Phase 61)

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-10

**A signal is not a backtest, so it gets its own table rather than another
column on `backtest_runs`.** Every table added since migration 0018 records
something that HAPPENED over a historical window - a run, a window, a
resampling, a perturbation, a scanned symbol. This one records a PRESENT-TENSE
claim: "as of the latest bar we hold for this symbol, these rules say BUY".
It has no date range, no starting cash, no equity curve and no trades, because
nothing was replayed and no capital was modelled; it has an `as_of_bar_date`
and a set of indicator values, because what it asserts is a state rather than
an outcome. Storing that as a degenerate backtest would mean a `backtest_runs`
row whose every metric column is NULL, which is exactly the shape this schema
reserves for a FAILED run.

**Append-only.** A row is written once and never updated, the discipline
`orders` states and `backtest_runs` inherits. This is an audit record of what
the engine said at time T from the data it had THEN; rewriting it later
against bars ingested since would erase the only evidence of the decision as
taken. A newer answer is a NEW row, ordered by `created_at`.

**Rows with `insufficient_data = true` are KEPT.** "This strategy could not be
evaluated for this symbol right now" is a real, recorded answer - the one that
later explains why nothing fired for a symbol somebody was watching. Dropping
those rows would leave a gap indistinguishable from never having asked, which
is the same no-fabrication reasoning (docs/TRADING_SAFETY.md) that keeps a
FAILED per-symbol row in `universe_scan_results` instead of quietly omitting
the symbol.

**`strategy_version_id` is `ON DELETE RESTRICT`**, the reasoning migrations
0018/0019/0021/0022 already gave: a persisted result's exact input must never
be able to disappear. A stored "BUY" with no retrievable rule set behind it is
precisely the black box spec section 25 forbids. `requested_by_user_id` is
`ON DELETE SET NULL`, matching every sibling - what was evaluated, and what it
said, outlives whoever asked for it.

One new enum type, `signaldirection` (`buy`, `sell`, `hold`) - the same three
values as the in-memory `Signal` the evaluator returns, deliberately declared
here as its own Postgres type rather than reusing a backtesting module's
vocabulary by reference, so a later change in that package cannot silently
reshape an audit table.

`entry_rule_held` and `exit_rule_held` are NULLABLE BOOLEANS, three-valued on
purpose: TRUE, FALSE, or NULL for "could not be evaluated at that bar" (an
indicator still inside its warmup, or a crossing operator with no previous
bar). That is the same `None`-means-cannot-evaluate rule
`apps/api/app/strategies/expressions.py` enforces in memory, carried through
to storage rather than flattened - collapsing NULL to FALSE would make a rule
nobody could check indistinguishable from one that was checked and did not
hold. `as_of_bar_date` and `latest_close` are nullable for the same reason and
only in the same case: zero ingested bars, where there is no bar to be "as of"
and no close to record. Neither is ever back-filled with the request time or a
last-known price.

`indicator_values` is JSONB - only the second JSONB column in this schema,
after `strategy_versions.definition` (migration 0017), and for a related
reason: its KEYS are the indicator ids of a user-authored definition, so there
is no fixed set of columns that could hold them. Decimals go in as STRINGS
(`{"sma_20": "105.50"}`), never JSON numbers, because JSON numbers are IEEE
floats and this codebase is Decimal end to end; `null` is preserved and means
"this indicator had no value at that bar", never `0`. Together with
`explanation` (a plain `String(1000)`, sized for one or two sentences and not
for a report) and the version's frozen `definition`, the row is enough to
re-derive the verdict by hand without re-running anything - which is what
spec sections 25 and 52 ask for.

`ix_signal_evaluations_version_symbol_created` supports the only listing
query: "this version's signals, optionally for one symbol, newest first". The
symbol sits in the middle of the index rather than at the end so the same
index serves the filtered and unfiltered forms of that listing.

Downgrade drops the table, then the enum type. Nothing else in the schema
references it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    signal_direction_enum = postgresql.ENUM(
        "buy",
        "sell",
        "hold",
        name="signaldirection",
        create_type=False,
    )
    signal_direction_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "signal_evaluations",
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
        sa.Column("as_of_bar_date", sa.Date(), nullable=True),
        sa.Column("latest_close", sa.Numeric(20, 6), nullable=True),
        sa.Column("signal", signal_direction_enum, nullable=False),
        sa.Column("entry_rule_held", sa.Boolean(), nullable=True),
        sa.Column("exit_rule_held", sa.Boolean(), nullable=True),
        sa.Column("insufficient_data", sa.Boolean(), nullable=False),
        sa.Column("indicator_values", postgresql.JSONB(), nullable=False),
        sa.Column("explanation", sa.String(length=1000), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_signal_evaluations_version_symbol_created",
        "signal_evaluations",
        ["strategy_version_id", "symbol", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_signal_evaluations_version_symbol_created", table_name="signal_evaluations"
    )
    op.drop_table("signal_evaluations")
    postgresql.ENUM(name="signaldirection").drop(op.get_bind(), checkfirst=True)
