"""DTOs for the order/fill history routes (docs/DECISIONS.md D065).

Read-only projections of the `orders` and `fills` rows Phase 4 has been
writing since the OMS existed. Every field below is a column that is
actually persisted - nothing here is derived, defaulted, or filled in
when the database has no answer, because these responses are the audit
trail (spec Sec17) and an audit trail that quietly invents a value is
worse than one that is missing.

Two things a reader of these shapes should know up front:

1. **There is no order-type column.** `orders` records `side`,
   `quantity`, `estimated_price` and an optional `stop_price`; it has no
   `order_type`/`time_in_force`. The paper broker fills at the
   proposal's `estimated_price` and the live adapter submits the same
   shape, so "market, with an advisory stop distance" is the only order
   type this system has ever produced. Rather than emit a constant
   `"market"` field that would look like a recorded fact, these schemas
   omit it entirely - see D065.

2. **`broker_kind` comes from the broker row, not from the order.**
   `orders` has no paper/live column (`Broker.kind` is the one
   discriminator, spec Sec51/D058), so the value carried on every row
   here is the joined `brokers.kind` for the broker the row belongs to.
   It is on every row rather than once per response so a blotter can
   label each line honestly even if a future client merges pages from
   several brokers. It is a real join, never an inference from which
   adapter "probably" ran.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from apps.api.app.db.models import BrokerKind, OrderStatus
from apps.api.app.risk.models import Side


class FillEntry(BaseModel):
    """One recorded execution (`fills`). An order has 0 fills when it was
    rejected (by the Risk Engine or the Portfolio Manager) and 1 when it
    filled; `fills.order_id` is UNIQUE today, so more than one is not
    currently reachable. The field is a list anyway because that is the
    shape the schema was deliberately built for (see `Fill`'s docstring:
    partial fills are meant to be a schema-compatible addition), and a
    client written against a list will not need changing when they
    arrive."""

    id: uuid.UUID
    order_id: uuid.UUID
    quantity: Decimal
    fill_price: Decimal
    filled_at: datetime


class OrderResponse(BaseModel):
    """One `orders` row plus its fills.

    Used by BOTH the list and the single-order detail route, so an order
    read one way can never disagree with the same order read the other -
    same reasoning as `_to_history_entry` in routes/portfolio.py.

    `quantity` is always the quantity actually acted on and
    `portfolio_requested_quantity` is what the Risk Engine had approved
    before the Portfolio Manager saw it: the two differ exactly when
    `portfolio_action` is `modify` (D029). Both are surfaced because a
    resize that is invisible in the history is exactly what D029 refused
    to create.

    `status` is `filled` or `rejected` - there is no pending state,
    because an order row is written once, after the decision, and is
    never updated (spec Sec17). A `rejected` row carries
    `risk_block_reason`/`risk_detail` when the Risk Engine stopped it and
    `portfolio_action: "reject"` when the Portfolio Manager did; null
    `portfolio_action` means the Portfolio Manager never ran, never that
    it approved.
    """

    id: uuid.UUID
    broker_id: uuid.UUID
    broker_kind: BrokerKind
    """`brokers.kind` for `broker_id`, joined - not a column on `orders`.
    See this module's docstring."""
    symbol: str
    side: Side
    quantity: Decimal
    estimated_price: Decimal
    """The price the proposal was evaluated at - the requested price. The
    executed price lives on the fill, and the two are equal under the
    paper broker by construction."""
    stop_price: Decimal | None
    status: OrderStatus
    risk_block_reason: str | None
    risk_detail: str | None
    portfolio_action: str | None
    portfolio_binding_constraint: str | None
    portfolio_detail: str | None
    portfolio_requested_quantity: Decimal | None
    submitted_by_user_id: uuid.UUID | None
    submitted_at: datetime
    fills: list[FillEntry]


class ListOrdersResponse(BaseModel):
    """Same `{items, limit, offset}` envelope as D027's portfolio history,
    D031's admin listings and D034's broker discovery - one pagination
    convention in this codebase, not four."""

    orders: list[OrderResponse]
    limit: int
    offset: int


class FillBlotterEntry(FillEntry):
    """A fill plus the order columns needed to read it on its own line.

    The flat fills listing exists for a blotter view - one row per actual
    execution - so each row repeats the parent order's `symbol`/`side`
    rather than forcing a client to join back against the orders listing.
    `order_id` is still carried, so a row can always be traced to the
    decision that produced it.
    """

    broker_id: uuid.UUID
    broker_kind: BrokerKind
    symbol: str
    side: Side


class ListFillsResponse(BaseModel):
    fills: list[FillBlotterEntry]
    limit: int
    offset: int
