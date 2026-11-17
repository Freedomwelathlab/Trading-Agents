"""Strategy Library paper bots (Phase 109, D136).

Revision ID: 0040
Revises: 0039

Two new tables and nothing else: `library_bots` (a paper sub-account that
holds what one library strategy says to hold) and `library_bot_runs` (one
append-only row per cycle). No existing table is altered.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "library_bots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "broker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("brokers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("strategy_id", sa.String(64), nullable=False),
        sa.Column("symbols", postgresql.ARRAY(sa.String(32)), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("capital", sa.Numeric(24, 8), nullable=False),
        sa.Column("max_drawdown_pct", sa.Numeric(8, 4)),
        sa.Column("cash", sa.Numeric(24, 8), nullable=False),
        sa.Column("positions", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("target", postgresql.JSONB()),
        sa.Column("last_decision_date", sa.Date()),
        sa.Column("last_equity", sa.Numeric(24, 8)),
        sa.Column("peak_equity", sa.Numeric(24, 8)),
        sa.Column(
            "requested_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "approved_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("paused_reason", sa.String(500)),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_library_bots_owner_user_id", "library_bots", ["owner_user_id"])
    op.create_table(
        "library_bot_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("library_bots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("orders_filled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("orders_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("equity", sa.Numeric(24, 8)),
        sa.Column("decision_date", sa.Date()),
        sa.Column("target", postgresql.JSONB()),
        sa.Column("orders", postgresql.JSONB()),
        sa.Column("detail", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_library_bot_runs_bot_id", "library_bot_runs", ["bot_id"])


def downgrade() -> None:
    op.drop_index("ix_library_bot_runs_bot_id", table_name="library_bot_runs")
    op.drop_table("library_bot_runs")
    op.drop_index("ix_library_bots_owner_user_id", table_name="library_bots")
    op.drop_table("library_bots")
