"""Broker provider names stored as registry keys (Phase 106, D128).

Revision ID: 0038
Revises: 0037

A broker row created with provider `Longport` failed every broker page with
UNKNOWN_PROVIDER, because the registry key is `longbridge` and the lookup
was case-sensitive. New rows are now normalised on the way in; this
rewrites existing rows the same way, and only when the result is a
provider the registry knows - an unrecognised name is left as it is so the
refusal still names what was typed.

Downgrade is a no-op: the original spelling is not worth a column, and
every name this rewrites was already unusable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies, so this migration means the same thing after the registry
# grows. Kept in step with execution/registry.py as of Phase 106.
_KNOWN = {"binance", "ibkr", "ig", "kraken", "longbridge", "moomoo", "paper"}
_ALIASES = {
    "longport": "longbridge",
    "long-port": "longbridge",
    "interactive-brokers": "ibkr",
    "interactivebrokers": "ibkr",
    "ig-markets": "ig",
    "igmarkets": "ig",
    "futu": "moomoo",
    "coinbase-paper": "paper",
}


def _canonical(name: str) -> str:
    key = "-".join(name.strip().lower().replace("_", " ").split())
    return _ALIASES.get(key, key)


def upgrade() -> None:
    bind = op.get_bind()
    # broker_credentials copies its broker's provider, so it is fixed alongside.
    for table in ("brokers", "broker_credentials"):
        rows = bind.execute(sa.text(f"SELECT id, provider FROM {table}")).fetchall()
        for row_id, provider in rows:
            fixed = _canonical(provider or "")
            if fixed != provider and fixed in _KNOWN:
                bind.execute(
                    sa.text(f"UPDATE {table} SET provider = :p WHERE id = :i"),
                    {"p": fixed, "i": row_id},
                )


def downgrade() -> None:
    pass
