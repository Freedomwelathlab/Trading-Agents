"""autotrade: both directions + a separate bar for extended hours (Phase 87, D106)

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-23

**`autotrade_bots.allow_short`** — off for every existing bot and off by
default for new ones. A bot approved by a human to trade long-only must
not start shorting because the platform learned how; enabling it is a
fresh operator decision on a bot that is already theirs.

**`autotrade_bot_trades.direction`** — `long` or `short`. Backfilled to
`long`, which is not a guess: until this migration the scanner only ever
emitted long signals and the paper broker refused a sell beyond the held
quantity, so every row that exists is a long by construction. The column
is what lets the stop live on the correct side of the entry, and what lets
the learning loop report a short's expectancy separately rather than
averaging two different trades together.

**`autotrade_bots.extended_hours_min_score`** — nullable, and NULL means
"hold extended-hours signals to the same bar as regular-session ones",
which is exactly what a `market_type='auto'` bot did before this column
existed. Existing bots therefore do not change behaviour. New bots get a
stricter figure from the API, because extended-hours prints are thin —
measured on the window this platform's setups were built against, TQQQ
traded roughly 1.9M shares pre-market to 53M in the regular session — so
the same score carries materially less evidence at 04:30 than at 10:30.

DOWNGRADE drops all three columns. A bot that had been shorting keeps its
open positions in `autotrade_bot_trades`, which after the downgrade no
longer records which side they were: do not downgrade with a short open.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "autotrade_bots",
        sa.Column(
            "allow_short", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "autotrade_bots",
        sa.Column("extended_hours_min_score", sa.Integer(), nullable=True),
    )
    op.add_column(
        "autotrade_bot_trades",
        sa.Column("direction", sa.String(8), nullable=True),
    )
    op.execute("UPDATE autotrade_bot_trades SET direction = 'long' WHERE direction IS NULL")
    op.alter_column(
        "autotrade_bot_trades",
        "direction",
        nullable=False,
        server_default=sa.text("'long'"),
    )


def downgrade() -> None:
    op.drop_column("autotrade_bot_trades", "direction")
    op.drop_column("autotrade_bots", "extended_hours_min_score")
    op.drop_column("autotrade_bots", "allow_short")
