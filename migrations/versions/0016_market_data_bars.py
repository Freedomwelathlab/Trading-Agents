"""market_data_bars + market_data_backfill_jobs - persisted historical
OHLCV bars and the audit trail of manual ingestion attempts that populate
them (Phase 53)

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-08

See docs/DECISIONS.md D070 for the full reasoning. In short:

`market_data_bars` closes the single biggest infrastructure gap blocking
every later Strategy Lab phase: apps/api/app/marketdata/history_provider.py
(D021) only ever exposes "the most recent N daily closes as of now," fetched
live from the vendor on every call, with no persistence and no arbitrary
historical window. This table is a real, queryable, idempotently-ingested
bar store instead.

Primary key is the natural composite `(symbol, bar_interval, ts)`, not this
schema's usual random-UUID `_uuid_pk()` - the one deliberate departure from
that helper in the whole schema. A Timescale hypertable's unique/primary
constraints must include the partitioning column (`ts`), and a surrogate
UUID PK would add nothing a natural key doesn't already give: idempotent
re-ingestion via `ON CONFLICT (symbol, bar_interval, ts) DO UPDATE`
(apps/api/app/marketdata/store.py), and no possibility of two rows ever
describing the same bar.

`bar_interval` is a plain VARCHAR, not a Postgres ENUM - so adding an
intraday interval later (5m, 1h, ...) is an application change, not a
migration that mutates a type every existing row depends on. Only `'1d'`
is ever written by Phase 53; the column exists for future use.

This is the first hypertable in this codebase, even though the Postgres
image has been `timescale/timescaledb` since Phase 1 - `CREATE EXTENSION`
has never been needed until there was a genuinely time-series table to
partition. `chunk_time_interval => INTERVAL '90 days'` is a reasonable
default for daily bars (roughly one chunk per ingested symbol per calendar
quarter); revisit once intraday intervals exist and this default no longer
fits every interval this table stores.

`market_data_backfill_jobs` is the audit trail of every manual, on-demand
ingestion attempt (see apps/api/app/marketdata/ingestion/backfill.py) - a
job row is written once and updated exactly once, in place, from
PENDING/RUNNING to a terminal SUCCEEDED/FAILED, since the job runs to
completion inside the HTTP request that created it; there is no background
worker in this codebase (outside the snapshot scheduler's own documented
single-worker limitation) that could race that update. `requested_by_user_id`
is ON DELETE SET NULL, matching `orders.submitted_by_user_id` and
`emergency_stop_events.actor_user_id` - this is an audit-adjacent
operational record whose meaning survives the deletion of whoever requested
it, unlike a Watchlist's CASCADE.

Downgrade drops both tables and the `backfilljobstatus` enum type, mirroring
migration 0001's enum-drop pattern. `market_data_bars` is dropped as a plain
`DROP TABLE` - Timescale hypertables need no special drop function, they are
ordinary tables under the extension's partitioning.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")

    op.create_table(
        "market_data_bars",
        sa.Column("symbol", sa.String(length=32), primary_key=True),
        sa.Column(
            "bar_interval", sa.String(length=8), primary_key=True, server_default="1d"
        ),
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("open", sa.Numeric(20, 6), nullable=True),
        sa.Column("high", sa.Numeric(20, 6), nullable=True),
        sa.Column("low", sa.Numeric(20, 6), nullable=True),
        sa.Column("close", sa.Numeric(20, 6), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_market_data_bars_symbol_interval_ts",
        "market_data_bars",
        ["symbol", "bar_interval", "ts"],
    )
    # Must run AFTER create_table: create_hypertable() partitions an
    # existing, empty table by `ts` rather than creating one itself.
    op.execute(
        "SELECT create_hypertable('market_data_bars', 'ts', "
        "chunk_time_interval => INTERVAL '90 days');"
    )

    backfill_status_enum = postgresql.ENUM(
        "pending", "running", "succeeded", "failed", "partial",
        name="backfilljobstatus",
        create_type=False,
    )
    backfill_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "market_data_backfill_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("bar_interval", sa.String(length=8), nullable=False, server_default="1d"),
        sa.Column("requested_start_date", sa.Date(), nullable=False),
        sa.Column("requested_end_date", sa.Date(), nullable=False),
        sa.Column("status", backfill_status_enum, nullable=False),
        sa.Column("bars_ingested", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("earliest_bar_date", sa.Date(), nullable=True),
        sa.Column("latest_bar_date", sa.Date(), nullable=True),
        sa.Column("error_detail", sa.String(length=500), nullable=True),
        sa.Column(
            "requested_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_backfill_jobs_symbol_interval", "market_data_backfill_jobs", ["symbol", "bar_interval"]
    )
    op.create_index("ix_backfill_jobs_status", "market_data_backfill_jobs", ["status"])


def downgrade() -> None:
    op.drop_table("market_data_backfill_jobs")
    postgresql.ENUM(name="backfilljobstatus").drop(op.get_bind(), checkfirst=True)
    op.drop_table("market_data_bars")
