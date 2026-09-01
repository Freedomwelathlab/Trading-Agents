"""Broker adapter port. Every concrete broker (paper today, a real broker
API later) implements this Protocol. Nothing outside this package should
import a concrete adapter directly - depend on BrokerAdapter so a paper
adapter and a live adapter are structurally interchangeable, never mixed up
by accident (see apps/api/app/core/execution_context.py for the credential
side of that same guarantee).
"""

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel

from apps.api.app.risk.models import AccountState, Side


class OrderRequest(BaseModel):
    """A market order. Limit orders are intentionally not supported yet -
    faking limit-order matching without a real order book would violate the
    no-fabrication rule (spec Sec57)."""

    symbol: str
    side: Side
    quantity: Decimal


class Fill(BaseModel):
    symbol: str
    side: Side
    quantity: Decimal
    fill_price: Decimal
    filled_at: datetime


class BrokerAdapter(Protocol):
    def submit_order(self, order: OrderRequest, *, market_price: Decimal) -> Fill: ...

    def get_account_state(self, *, marks: dict[str, Decimal]) -> AccountState: ...

    @property
    def cash(self) -> Decimal: ...

    @property
    def positions(self) -> dict[str, Decimal]:
        """A COPY - a caller must never mutate a broker's positions except
        through submit_order(). Promoted into the Protocol in Phase 43
        (D058): the trade route needs both of these to build the Portfolio
        Manager's view of the book, so they were always part of the real
        contract; before a second adapter existed, only the concrete
        PaperBrokerAdapter happened to declare them."""
        ...
