"""autotrade learning loop: adverse excursion + daily insights (Phase 83, D099)

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-22

**`autotrade_bot_trades.trough_price`** — the lowest low seen since entry,
the mirror of `peak_price`. Together they are the trade's maximum
favourable and adverse excursion, which is what turns "the stop was hit"
into a question with an answer: did the trade first reach +1R and give it
back (a trailing-stop question), or go straight to the stop (an entry
question)? Backfilled to the entry price for existing rows — the honest
value for a trade whose path was not recorded.

**`autotrade_bot_insights`** — one row per bot per session, written by the
engine after the session it describes has ended, holding the day's
aggregate and a list of FINDINGS: deterministic, thresholded statements
("60% of stopped trades reached +1R first", "score-3 signals lose while
score-4+ win") that the operator reads and decides on. Nothing here
changes a bot's parameters by itself; the only automatic consequence the
loop has is the existing setup demotion. The row is the bot's memory of
what it learned, kept where it can be audited.

DOWNGRADE drops the table and the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032"
down_revision = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "autotrade_bot_trades",
        sa.Column("trough_price", sa.Numeric(20, 6), nullable=True),
    )
    op.execute("UPDATE autotrade_bot_trades SET trough_price = entry_price WHERE trough_price IS NULL")
    op.alter_column("autotrade_bot_trades", "trough_price", nullable=False)

    op.create_table(
        "autotrade_bot_insights",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("autotrade_bots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("trades", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column("total_r", sa.Numeric(12, 4), nullable=False),
        sa.Column("total_pnl", sa.Numeric(24, 8), nullable=False),
        sa.Column("best_setup", sa.String(length=32)),
        sa.Column("worst_setup", sa.String(length=32)),
        sa.Column(
            "findings", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"
        ),
        sa.Column(
            "demoted_setups",
            postgresql.ARRAY(sa.String(length=32)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("bot_id", "session_date", name="uq_autotrade_insight_bot_session"),
    )


def downgrade() -> None:
    op.drop_table("autotrade_bot_insights")
    op.drop_column("autotrade_bot_trades", "trough_price")
