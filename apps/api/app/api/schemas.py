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
from apps.api.app.portfolio_manager.models import PortfolioAction, PortfolioConstraint
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

    confirm: bool = False
    """Phase 43 (docs/DECISIONS.md D058): per-request explicit confirmation,
    REQUIRED for a trade against a LIVE broker and ignored entirely for a
    paper one.

    Defaults to false so that a body which would place a paper trade can
    never place a live one merely by being pointed at a different broker
    id. Omitting it (or sending false) against a live broker is a 400, not
    a silently-approved trade.

    This is a structural safeguard, not a UX nicety: docs/TRADING_SAFETY.md
    requires explicit confirmation before a real order, and a confirmation
    that lives only in a frontend dialog is not one the server can enforce.
    Any caller - a script, an agent, a retried request, a future UI - has
    to state its intent to spend real money in the payload itself."""


class TradeSubmissionResponse(BaseModel):
    order_id: uuid.UUID
    status: OrderStatus
    approved: bool
    """The RISK Engine's verdict specifically. `approved: true` with
    `status: rejected` is a real, meaningful combination as of D029: the
    Risk Engine passed the trade and the Portfolio Manager then rejected
    it - read `portfolio_action` to tell the two apart."""
    block_reason: BlockReason | None
    detail: str | None
    portfolio_action: PortfolioAction | None
    """The trade-path Portfolio Manager's verdict (D029). Null means it
    never ran - the Risk Engine rejected the proposal first. Never read
    null as approval."""
    portfolio_binding_constraint: PortfolioConstraint | None
    portfolio_detail: str | None
    portfolio_requested_quantity: Decimal | None
    """The quantity submitted to the Portfolio Manager. Differs from
    fill_quantity exactly when portfolio_action is 'modify'."""
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
    """What the TraderAgent proposed - not necessarily what traded. The
    Portfolio Manager may have shrunk it (D029); `fill_quantity` is what
    actually reached the broker."""
    rationale: str
    """The agent's one-sentence explanation - informational only, never
    itself validated or acted on (docs/AGENT_POLICY.md)."""
