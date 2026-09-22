"""HTTP-facing DTOs for the trades endpoint. Deliberately separate from the
domain models in risk/models.py and oms/service.py - the HTTP contract can
evolve (renamed fields, added validation, versioning) without forcing a
matching change on the pure domain layer, and vice versa.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from apps.api.app.agents.technical_analyst import Stance
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
    order_type: Literal["market", "limit"] = "market"
    limit_price: Decimal | None = Field(default=None, gt=0)
    """Phase 84 (D101). For a limit order the risk engine sizes on the
    limit price (a buy cannot pay more, a sell cannot get less), so
    `estimated_price` may be omitted. Paper brokers fill only a MARKETABLE
    limit, at the market; a non-marketable one is a 409, never a pretend
    resting order. Live brokers send a real limit order the venue may
    rest; that comes back as an unconfirmed order to cancel or reconcile."""
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


class AnalystReadOut(BaseModel):
    """The stance/summary/confidence contract every analyst read shares
    (Phase 52). Mirrors TechnicalRead/FundamentalRead/NewsRead field for
    field - those three models are already deliberately identical
    (docs/DECISIONS.md D059), so this HTTP projection is one shape rather
    than three near-copies.

    Read-only by construction, exactly like the domain models it mirrors:
    no side, quantity, price or stop field exists here either, so nothing
    in an analyst read can be mistaken for a trade proposal by a consumer
    of this response any more than it can be inside the server.
    """

    stance: Stance
    summary: str
    confidence: Decimal


class TechnicalAnalystReadOut(AnalystReadOut):
    indicator_context: str | None
    """The verbatim indicator string the analyst was handed - the real
    `SMA(20)=...`/`RSI(14)=...` values computed deterministically by
    apps/api/app/marketdata/indicators.py (never by the LLM, D021).

    Null when no HistoryProvider is configured, when the vendor call
    failed, or when the series was too short for either window. Null is
    the honest answer in all three cases: the analyst then commented
    qualitatively on the single live quote alone, and no indicator value
    it wasn't given exists to report."""


class FundamentalAnalystReadOut(AnalystReadOut):
    data_source: str
    """Which FundamentalsProvider produced the figures this read narrates,
    verbatim off `CompanyFundamentals.source` - the same audit-trail
    reasoning as `MarketSnapshot.source`."""
    fundamentals_as_of: datetime | None
    """Timestamp of the most recent valuation data point the vendor
    actually returned. Null when the vendor dated nothing - never filled
    in with "now", which would misrepresent stale figures as fresh
    (`CompanyFundamentals.as_of`)."""


class NewsAnalystReadOut(AnalystReadOut):
    headline_count: int
    """How many real headlines this read is based on - counted in code
    from the exact list shown to the analyst, never a number the model
    stated. Always >= 1: an empty headline list is DATA_UNAVAILABLE at the
    provider layer and never reaches an analyst, so there is no read to
    return in that case at all."""


class AgentTradeResponse(TradeSubmissionResponse):
    side: Side
    quantity: Decimal
    """What the TraderAgent proposed - not necessarily what traded. The
    Portfolio Manager may have shrunk it (D029); `fill_quantity` is what
    actually reached the broker."""
    rationale: str
    """The agent's one-sentence explanation - informational only, never
    itself validated or acted on (docs/AGENT_POLICY.md)."""

    technical_analyst: TechnicalAnalystReadOut | None
    fundamental_analyst: FundamentalAnalystReadOut | None
    news_analyst: NewsAnalystReadOut | None
    """Phase 52: what each of D059's three analysts actually returned on
    THIS request, closing the gap D062 recorded ("nothing about them is
    returned; the response can't even say whether any ran").

    Nullable PER ANALYST, not as a group, because that is the truth of the
    server-side design: the three run concurrently and are independently
    failure-isolated, so any combination of them may have produced a read.
    A null here means exactly one thing - THIS analyst produced no read on
    this request - and says nothing whatsoever about the other two.

    Null deliberately does NOT distinguish "not configured" from "ran and
    failed". Both are already handled identically everywhere else in the
    trade path (the context is simply omitted, the trade proceeds), and
    the reason is recorded in the server's structured logs. Encoding a
    reason string here would invite a consumer to render one, and a
    synthesised "no signal" read is precisely the fabrication spec Sec57
    forbids - a missing read is null, never a manufactured neutral stance
    with a zero confidence.

    These three fields are additive only. Every field above them means
    exactly what it meant before this phase, and no analyst can influence
    any of them: the Risk Engine and Portfolio Manager verdicts, the
    side/quantity/status the trade actually got, and the price are all
    produced by paths no analyst read touches."""
