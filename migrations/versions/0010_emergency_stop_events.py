"""emergency_stop_events - append-only, audited, live-flippable emergency
stop state (docs/DECISIONS.md D039)

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-29

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "emergency_stop_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # The only query this table ever serves on the trade path is "the
    # latest row", which Postgres answers from the PK index alone; no
    # extra index is created for it. This index exists for the audit read
    # (history, newest first, per actor) - see D039.
    op.create_index(
        "ix_emergency_stop_events_actor",
        "emergency_stop_events",
        ["actor_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_emergency_stop_events_actor", table_name="emergency_stop_events")
    op.drop_table("emergency_stop_events")
