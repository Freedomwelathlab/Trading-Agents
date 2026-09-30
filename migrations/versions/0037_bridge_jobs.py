"""bridge agent job queue and heartbeats (Phase 104, D124)

Revision ID: 0037
Revises: 0035
Create Date: 2026-09-29

IBKR (Client Portal Gateway) and moomoo (OpenD) are reachable only through
a gateway process on the operator's own Windows PC. The API runs on
Railway and cannot see that machine's localhost, so a small agent on the PC
PULLS work from the platform over an outbound HTTPS connection and posts
the gateway's answers back. These two tables are the whole server side of
that exchange.

`bridge_jobs` - one row per request the platform asks the agent to carry
out. The payload never holds a secret: the gateway session, the IBKR login
and the moomoo trade password all stay on the PC. `status` moves only
forward: queued -> claimed -> done | failed, or queued/claimed -> expired
when the platform stops waiting. A result that arrives for an expired job
is still written, for the audit trail, and the status stays `expired` - a
late answer to an order request is exactly the row an operator needs to
find.

`bridge_agent_heartbeats` - one row per agent id, upserted every ~15 s,
carrying which gateways that agent can reach and whether each is
authenticated. The adapter reads it BEFORE enqueuing anything, so "the PC
is off" or "the IBKR session expired overnight" is an immediate, specific
answer instead of a 25-second timeout.

Revision chain: 0036 is reserved for a parallel phase that had not landed
when this was written. If 0036 exists when this merges, change
`down_revision` below to "0036" - the two touch disjoint tables.

DOWNGRADE drops both tables and the job history in them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0037"
down_revision = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bridge_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column(
            "broker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("brokers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("claimed_by", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('account_read', 'positions_read', 'order_validate', "
            "'order_submit', 'order_status', 'cancel')",
            name="ck_bridge_jobs_kind",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'claimed', 'done', 'failed', 'expired')",
            name="ck_bridge_jobs_status",
        ),
    )
    # The claim query's only shape: the oldest queued job for a provider.
    op.create_index(
        "ix_bridge_jobs_queued",
        "bridge_jobs",
        ["provider", "created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index("ix_bridge_jobs_created_at", "bridge_jobs", ["created_at"])

    op.create_table(
        "bridge_agent_heartbeats",
        sa.Column("agent_id", sa.String(64), primary_key=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent_version", sa.String(32), nullable=True),
        sa.Column(
            "gateways",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_table("bridge_agent_heartbeats")
    op.drop_index("ix_bridge_jobs_created_at", table_name="bridge_jobs")
    op.drop_index("ix_bridge_jobs_queued", table_name="bridge_jobs")
    op.drop_table("bridge_jobs")
