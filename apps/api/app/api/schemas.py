"""HTTP-facing DTOs for the trades endpoint. Deliberately separate from the
domain models in risk/models.py and oms/service.py - the HTTP contract can
evolve (renamed fields, added validation, versioning) without forcing a
matching change on the pure domain layer, and vice versa.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from apps.api.app.db.models import OrderStatus
from apps.api.app.risk.models import BlockReason, Side


class TradeSubmissionRequest(BaseModel):
    symbol: str = Field(min_length=1)
    side: Side
    quantity: Decimal = Field(gt=0)
    estimated_price: Decimal | None = Field(default=None, gt=0)
    """Omit to have the server fetch a live quote from the configured
    market data vendor (D017) and use its price. If supplied, the
    caller's price is authoritative and no vendor is consulted - this is
    what every trade submission did before D017 and remains fully
    supported. 400 NOT_CONFIGURED / DATA_UNAVAILABLE if omitted and no
    vendor is wired or no data exists; the server never guesses a price
    itself."""
    stop_price: Decimal | None = Field(default=None, gt=0)
    market_data_as_of: datetime | None = None
    """Only meaningful alongside a caller-supplied estimated_price - omit
    to use the server's current time. Ignored (and overwritten by the
    vendor's own quote timestamp) when estimated_price is omitted, since
    mixing a live price with a caller-chosen timestamp would misrepresent
    when that price was actually observed."""
    marks: dict[str, Decimal] = Field(default_factory=dict)
    """Current price for every OTHER open position on this broker (not the
    symbol being traded, which uses estimated_price). Required to value
    the account's exposure; omitting a held symbol's mark fails the
    request rather than guessing a stale price (spec §57)."""


class TradeSubmissionResponse(BaseModel):
    order_id: uuid.UUID
    status: OrderStatus
    approved: bool
    block_reason: BlockReason | None
    detail: str | None
    fill_quantity: Decimal | None
    fill_price: Decimal | None


class AgentTradeRequest(BaseModel):
    """D018: the human supplies the symbol and a free-text directive; the
    TraderAgent proposes side/quantity/stop distance; the deterministic
    market-data path (D017) supplies the price. marks behaves identically
    to TradeSubmissionRequest.marks."""

    symbol: str = Field(min_length=1)
    directive: str = Field(min_length=1, max_length=500)
    marks: dict[str, Decimal] = Field(default_factory=dict)


class AgentTradeResponse(TradeSubmissionResponse):
    side: Side
    quantity: Decimal
    rationale: str
    """The agent's one-sentence explanation - informational only, never
    itself validated or acted on (docs/AGENT_POLICY.md)."""
