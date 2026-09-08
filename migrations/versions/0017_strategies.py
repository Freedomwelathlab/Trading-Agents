"""strategies + strategy_versions - user-owned strategy definitions and
their immutable-once-validated version history (Phase 54)

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-08

Two tables rather than one. A `definition jsonb` column directly on
`strategies` would have been shorter, but a backtest (Phase 55) has to name
the exact rule set it ran against, forever - `backtest_runs.strategy_version_id`
is planned as ON DELETE RESTRICT for precisely that reason. If the
definition lived on the strategy row, editing a strategy would silently
rewrite the input of every result already computed from it, and the audit
trail would be quietly, unrecoverably wrong. Splitting versions out makes
"the thing that was run" a separate, addressable row that nothing can edit
after the fact.

`strategy_versions.definition` is THE FIRST JSONB COLUMN IN THIS SCHEMA,
and that is a deliberate exception rather than a new default. Every other
table here uses typed columns and should continue to. The rule vocabulary
this column holds is still moving - walk-forward (Phase 57), Monte Carlo,
and universe scanning (Phase 59) each want new indicator and operator types
- so a typed schema would mean a migration per new rule type, each one
reshaping a table every existing strategy depends on. One JSONB column plus
application-level structural validation
(apps/api/app/strategies/validation.py) is the cheaper and more honest
trade. The validation is what keeps this from being a junk drawer: it
rejects unknown keys rather than ignoring them, so a typo is an itemized
error and not a silently dropped rule, and nothing is ever marked
`validated` without passing it.

**Immutability is enforced at the API layer, not by a database trigger.**
A version whose status is not `draft` cannot be edited: `PATCH
/strategies/{id}/versions/{version_id}` answers 409 and names the fork
endpoint to use instead. A trigger would enforce the same invariant, but
the failure a caller sees would be a raw driver exception rather than an
HTTP error that explains the alternative - and this invariant has exactly
one writer (the routes in apps/api/app/api/routes/strategies.py), so there
is no second path a trigger would be catching.

Two new enum TYPES, created and dropped explicitly with
`postgresql.ENUM(..., create_type=False)` + `.create()` / `.drop()`,
following migration 0001's pattern rather than letting `sa.Enum` create
them implicitly as a side effect of `create_table` (which leaves the
downgrade with an orphaned type):

- `strategystatus` (`active`, `archived`) - the strategy-level lifecycle.
- `strategyversionstatus` (`draft`, `validated`, `archived`) - the
  per-version one. These are genuinely different lifecycles on genuinely
  different rows, not one enum shared by two tables: a strategy is
  archived when a user retires it from their working list, while a version
  is validated when its rules pass structural checks. Sharing a type would
  have let a strategy be set to `draft`, which is meaningless.

Both are real Postgres ENUMs, unlike `market_data_bars.bar_interval`
(migration 0016) which is deliberately a plain VARCHAR. The difference is
whether the value set is expected to grow: bar intervals will grow (5m, 1h,
...), whereas these two lifecycles are closed - a third strategy status
would be a design change, not an incremental addition.

`ix_strategies_owner_created` supports the only listing query there is,
`GET /strategies` - "this user's strategies, oldest first". The unique
constraint on `(strategy_id, version_number)` doubles as the index for
"this strategy's versions", and guarantees version numbers name exactly one
row: they are dense, start at 1, and are assigned by the application as
`max(existing) + 1`.

`strategy_versions.strategy_id` is ON DELETE CASCADE (a version has no
meaning without its strategy, exactly like `watchlist_items.watchlist_id`).
Both user references are ON DELETE SET NULL, deliberately the opposite of
`watchlists.user_id`'s CASCADE: a strategy is not ephemeral personal state
the way a watchlist is, and Phase 55's ON DELETE RESTRICT from
`backtest_runs` means a version must be able to outlive its author. That
matches `orders.submitted_by_user_id` and
`market_data_backfill_jobs.requested_by_user_id`, not a Watchlist.

Downgrade drops `strategy_versions` first (it references `strategies` via
FK), then `strategies`, then both enum types - nothing else in the schema
references either table, so the round-trip is clean.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    strategy_status_enum = postgresql.ENUM(
        "active", "archived", name="strategystatus", create_type=False
    )
    strategy_status_enum.create(op.get_bind(), checkfirst=True)

    strategy_version_status_enum = postgresql.ENUM(
        "draft", "validated", "archived", name="strategyversionstatus", create_type=False
    )
    strategy_version_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "strategies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("status", strategy_status_enum, nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_strategies_owner_created", "strategies", ["owner_user_id", "created_at"])

    op.create_table(
        "strategy_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "strategy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("definition", postgresql.JSONB(), nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("status", strategy_version_status_enum, nullable=False, server_default="draft"),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("strategy_id", "version_number", name="uq_strategy_version_number"),
    )
    op.create_index(
        "ix_strategy_versions_strategy_created",
        "strategy_versions",
        ["strategy_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_strategy_versions_strategy_created", table_name="strategy_versions")
    op.drop_table("strategy_versions")
    op.drop_index("ix_strategies_owner_created", table_name="strategies")
    op.drop_table("strategies")
    postgresql.ENUM(name="strategyversionstatus").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="strategystatus").drop(op.get_bind(), checkfirst=True)
