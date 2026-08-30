"""Pydantic response models for a portfolio snapshot. Every money/quantity
field is Decimal, never float (project-wide rule for anything financial)."""

import enum
import uuid
from decimal import Decimal

from pydantic import BaseModel


class CostBasisMethod(str, enum.Enum):  # noqa: UP042 (str mixin kept for query-param interop)
    """Which cost-basis method a portfolio snapshot's `avg_cost`,
    `unrealized_pnl` and `realized_pnl` are computed under (D041).

    AVERAGE is the default and is byte-for-byte the behaviour D022 shipped -
    a single running weighted-average cost per symbol, never re-derived from
    individual lots. FIFO and LIFO instead track the open purchase lots and
    consume them oldest-first / newest-first on each sell, so the same fill
    history legitimately yields three different realized-P&L figures. That
    divergence is correct, not a bug: it is the whole point of offering the
    choice.

    A `str` enum so FastAPI accepts it directly as a query-string value
    (`?cost_basis_method=fifo`) and rejects anything else with a 422 rather
    than silently falling back to a default.
    """

    AVERAGE = "average"
    FIFO = "fifo"
    LIFO = "lifo"


class PortfolioPosition(BaseModel):
    symbol: str
    quantity: Decimal
    avg_cost: Decimal
    """Cost basis of the currently-held quantity, from replaying this
    symbol's fill history under the requested CostBasisMethod (see
    snapshot.py's module docstring). Under AVERAGE this is the running
    weighted-average cost, which sells never change; under FIFO/LIFO it is
    the weighted-average price of whatever open lots survived the sells, so
    it does move when a sell consumes lots."""
    current_value: Decimal
    """quantity * the caller-supplied mark for this symbol."""
    unrealized_pnl: Decimal
    """(mark - avg_cost) * quantity."""
    realized_pnl: Decimal
    """Cumulative realized P&L for this symbol from every fill ever made
    on it, not just the currently-open quantity - a symbol closed and
    reopened still carries its prior realized P&L forward."""


class PortfolioSnapshot(BaseModel):
    broker_id: uuid.UUID
    cash: Decimal
    positions: list[PortfolioPosition]
    total_equity: Decimal
    """cash + sum(current_value across positions)."""
    total_unrealized_pnl: Decimal
    """sum(unrealized_pnl across positions)."""
    total_realized_pnl: Decimal
    """sum(realized_pnl across every symbol ever traded on this broker,
    including symbols with no currently-open position."""
