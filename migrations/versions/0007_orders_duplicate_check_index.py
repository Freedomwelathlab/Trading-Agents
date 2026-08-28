"""orders composite index - supports the duplicate-order recent-orders query

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-28

"""
from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # docs/DECISIONS.md D024: get_recent_filled_orders() (oms/persistence.py)
    # filters on exactly this triple - broker_id equality, symbol equality,
    # submitted_at range - on every trade submission, so it needs its own
    # composite index rather than relying on the existing single-column
    # ix_orders_symbol index or the broker_id FK's implicit index alone.
    op.create_index(
        "ix_orders_broker_symbol_submitted",
        "orders",
        ["broker_id", "symbol", "submitted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_orders_broker_symbol_submitted", table_name="orders")
