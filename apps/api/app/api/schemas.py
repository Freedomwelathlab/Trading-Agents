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
    estimated_price: Decimal = Field(gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    market_data_as_of: datetime | None = None
    """Omit to use the server's current time. This is a timestamp
    convenience default, not a fabricated data value - the price and
    symbol always come from the caller, never invented server-side."""
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
