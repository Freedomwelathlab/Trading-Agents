"""Options bot signal mode: the minimum underlying scan score (Phase 107, D134).

Revision ID: 0039
Revises: 0038

Nullable: only a bot whose structure is `signal` reads it; every existing
bot opens a fixed structure and keeps NULL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("options_bots", sa.Column("min_signal_score", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("options_bots", "min_signal_score")
