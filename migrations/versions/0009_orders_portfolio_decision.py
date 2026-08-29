"""orders.portfolio_* - persisted audit record of the trade-path Portfolio
Manager's decision on each order (docs/DECISIONS.md D029, spec §18's
"complete audit record")

All four columns are nullable: every order written before this migration
predates the Portfolio Manager entirely, and even afterwards a risk-rejected
order never reaches it. Null therefore means "no portfolio decision was
made", never "approved" - see apps/api/app/db/models.py's Order docstrings.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-29

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("portfolio_action", sa.String(32), nullable=True))
    op.add_column(
        "orders", sa.Column("portfolio_binding_constraint", sa.String(64), nullable=True)
    )
    op.add_column("orders", sa.Column("portfolio_detail", sa.String(500), nullable=True))
    op.add_column(
        "orders", sa.Column("portfolio_requested_quantity", sa.Numeric(24, 8), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("orders", "portfolio_requested_quantity")
    op.drop_column("orders", "portfolio_detail")
    op.drop_column("orders", "portfolio_binding_constraint")
    op.drop_column("orders", "portfolio_action")
