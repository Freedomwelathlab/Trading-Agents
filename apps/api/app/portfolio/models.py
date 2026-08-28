"""Pydantic response models for a portfolio snapshot. Every money/quantity
field is Decimal, never float (project-wide rule for anything financial)."""

import uuid
from decimal import Decimal

from pydantic import BaseModel


class PortfolioPosition(BaseModel):
    symbol: str
    quantity: Decimal
    avg_cost: Decimal
    """Average cost basis of the currently-held quantity, from replaying
    this symbol's fill history (see snapshot.py's module docstring for the
    method and why it was chosen)."""
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
