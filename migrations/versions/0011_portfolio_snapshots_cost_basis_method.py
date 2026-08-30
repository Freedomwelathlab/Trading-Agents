"""portfolio_snapshots.cost_basis_method - record which cost-basis method
produced each persisted snapshot (docs/DECISIONS.md D044)

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-30

`server_default='average'` is the whole point of this migration, not an
incidental convenience: every row that already exists was written by the
average-only write path D041 deliberately scoped it to, so backfilling
them as 'average' records a fact rather than a guess. A nullable column
would have made those rows read as "method unknown", which is less
accurate than what we actually know.

The default is kept on the column (not dropped after the backfill) so an
INSERT from any older code path that predates the model change still lands
a truthful, non-null value instead of failing or writing a blank.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "portfolio_snapshots",
        sa.Column(
            "cost_basis_method",
            sa.String(length=16),
            nullable=False,
            server_default="average",
        ),
    )


def downgrade() -> None:
    op.drop_column("portfolio_snapshots", "cost_basis_method")
