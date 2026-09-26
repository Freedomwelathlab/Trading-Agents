"""Broker adapter port. Every concrete broker (paper today, a real broker
API later) implements this Protocol. Nothing outside this package should
import a concrete adapter directly - depend on BrokerAdapter so a paper
adapter and a live adapter are structurally interchangeable, never mixed up
by accident (see apps/api/app/core/execution_context.py for the credential
side of that same guarantee).
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel

from apps.api.app.risk.models import AccountState, Side


class OrderRequest(BaseModel):
    """A market order, or (Phase 84, D101) a limit order.

    A limit order at a real venue may REST unfilled. The paper broker has
    no order book to rest it in, so it fills a MARKETABLE limit (a buy at
    or above the market, a sell at or below it) at the market price — the
    real outcome of such an order — and refuses a non-marketable one with
    `OrderWouldRestError` rather than inventing a later fill (spec Sec57).
    The live adapter sends `OrderType.LO` and reports the venue's answer."""

    symbol: str
    side: Side
    quantity: Decimal
    order_type: Literal["market", "limit"] = "market"
    limit_price: Decimal | None = None


class OrderWouldRestError(Exception):
    """A limit order that would not execute at the current price. The paper
    broker raises it instead of pretending to hold a resting order; the
    route turns it into a 409 with the numbers in it."""


class Fill(BaseModel):
    symbol: str
    side: Side
    quantity: Decimal
    fill_price: Decimal
    filled_at: datetime


class UnconfirmedSubmissionContext(BaseModel):
    """What was already decided when the broker accepted an order it has
    not executed. Carried on `OrderNotConfirmedError` so the persistence
    layer can record a truthful `orders` row for a real order instead of
    either losing it or guessing its quantity.

    `effective_quantity` is deliberately the quantity ACTUALLY handed to
    the broker, which differs from the caller's proposal whenever the
    Portfolio Manager shrank it (D029). Recording the proposal's quantity
    here would put a number in the audit trail that was never sent to
    anyone."""

    effective_quantity: Decimal
    risk_detail: str | None = None
    portfolio_action: str | None = None
    portfolio_binding_constraint: str | None = None
    portfolio_detail: str | None = None
    portfolio_requested_quantity: Decimal | None = None


class VenueLoginRefusedError(Exception):
    """A venue refused to AUTHENTICATE the credentials (Phase 98, D117).

    Distinct from every other venue error because repeating it has a cost
    the others do not: brokers count failed API logins and suspend the
    client after enough of them. IG did exactly that to this platform's
    demo key on 2026-09-25 (`error.security.client-suspended`) after a run
    of `invalid-details` refusals. The connection probe therefore stops
    retrying a set that raised this until an operator forces it."""


class OrderNotConfirmedError(Exception):
    """A broker ACCEPTED a real order but has not (yet) reported executing
    any of it, so no `Fill` can honestly be returned.

    Defined at the port rather than on a concrete adapter (Phase 49,
    docs/DECISIONS.md D066) for one reason: `apps/api/app/oms/service.py`
    has to recognise this condition in order to attach the decision context
    that produced the order (see `submission_context` below), and the pure,
    DB-free OMS must not import a concrete live adapter to do it. Any future
    adapter with the same "accepted, outcome unknown" state raises this and
    inherits the reconciliation path for free.

    `LiveOrderNotFilledError` (apps/api/app/execution/live_broker.py)
    subclasses this, so every pre-existing `except LiveOrderNotFilledError`
    keeps catching exactly what it always did.
    """

    def __init__(self, message: str, *, order_id: str) -> None:
        super().__init__(message)
        self.order_id = order_id
        """The BROKER's own order id - the only handle by which the order
        can later be reconciled. Never synthesised locally."""
        self.submission_context: UnconfirmedSubmissionContext | None = None
        """Filled in by `submit_trade()` on the way out, never by an
        adapter. An adapter knows the order id; only the OMS knows which
        risk/portfolio decision produced the order and at what quantity it
        was ultimately submitted, and that is exactly what an honest audit
        row for a real, money-moving order needs. Stays None if this
        exception is raised outside `submit_trade()` - in which case the
        caller must NOT invent the missing fields."""


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
