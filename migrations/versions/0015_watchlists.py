"""watchlists + watchlist_items - user-scoped symbol lists behind
GET/POST/DELETE /watchlists and GET /watchlists/{id}/quotes (Phase 50)

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-03

Two tables rather than one with an array column. A `symbols text[]` on
`watchlists` would have been shorter, but it makes the one invariant this
feature actually has - a symbol appears at most once per list - an
application-level check rather than a database constraint, and the whole
point of `uq_watchlist_item_watchlist_symbol` below is that two concurrent
adds of the same symbol cannot both win. It also keeps the door open for
per-item columns (a note, a target price, when it was added and by which
path) without a migration that rewrites every row's array.

Both foreign keys are ON DELETE CASCADE, for two different reasons:

- `watchlist_items.watchlist_id`: an item has no meaning without its list.
  Deleting a watchlist over `DELETE /watchlists/{id}` therefore removes its
  items in one statement in the database rather than in a loop in the
  application that could half-finish.
- `watchlists.user_id`: a watchlist is that user's own working research
  state and must not outlive them. This mirrors `password_reset_tokens`
  (D063) and is deliberately the opposite of `orders.submitted_by_user_id`,
  which is nullable precisely so append-only trade history survives the
  deletion of its submitter. Note this codebase does not delete users at
  all (D013 documents deactivation as the path), so the cascade is a
  structural guarantee rather than a routine code path.

`ix_watchlists_user_created` supports the only listing query there is -
"this user's watchlists, oldest first". The unique constraint on
`(watchlist_id, symbol)` doubles as the index for "this list's items".

Symbols are stored normalized (trimmed, upper-cased) by the route layer,
so the uniqueness constraint is real rather than case-dodgeable. Nothing
in this migration validates a symbol against a vendor: a watchlist is
research state, and being able to add a symbol the system has never traded
is the feature, not a gap. `GET /watchlists/{id}/quotes` is where an
unknown symbol becomes visible - as a DATA_UNAVAILABLE row, never as a
fabricated price.

Renumbered 0014->0015 at merge time: this worktree branched from `main`
before Phase 49 (D066) merged and also claimed 0014 (`orders.broker_order_id`
etc.); Phase 49 merged first, so this migration takes 0015 and chains onto
its 0014 instead of 0013, following the same D028/D030/D032 convention
used for colliding D-numbers.

Downgrade drops both tables (items first, since it references watchlists).
No other table references either of these, so the round-trip is clean.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "watchlists",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_watchlists_user_created", "watchlists", ["user_id", "created_at"])

    op.create_table(
        "watchlist_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "watchlist_id",
            UUID(as_uuid=True),
            sa.ForeignKey("watchlists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("watchlist_id", "symbol", name="uq_watchlist_item_watchlist_symbol"),
    )


def downgrade() -> None:
    op.drop_table("watchlist_items")
    op.drop_index("ix_watchlists_user_created", table_name="watchlists")
    op.drop_table("watchlists")
