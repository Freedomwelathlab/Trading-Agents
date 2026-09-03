"""Read-only order and fill history (docs/DECISIONS.md D065).

The gap Phase 47 (D062) named: `orders` and `fills` have been written on
every trade since Phase 4, but nothing exposed them, so the platform kept
an append-only audit trail that no user could read back through the
product.

This module is READ-ONLY by construction, and deliberately separate from
routes/trades.py. That module's docstring opens by claiming to hold "the
only HTTP entrypoints that can move a trade toward the broker"; putting a
listing route in it would make that sentence false at a glance even
though the route is harmless. Nothing here opens a session for writing,
constructs a broker adapter, touches the Risk Engine, the Portfolio
Manager, the emergency stop, or `LIVE_TRADING_ENABLED` - it issues
`SELECT`s and returns rows. Same posture, and the same reason, as
routes/portfolio.py being separate from routes/trades.py.

Authorization is `require_broker_access(Permission.VIEW_PORTFOLIO)` -
literally the dependency every other broker-scoped read in this codebase
uses, not a new scheme. VIEW_PORTFOLIO rather than SUBMIT_PAPER_TRADE for
D022's stated reason: reading what a broker did is strictly weaker than
moving money in it, and a read-only dashboard user should not need
trade-submission rights to see a trade blotter. No new Permission member
was added: an order listing is the history behind exactly the positions
and P&L `portfolio:view` already authorizes, so a separate permission
would draw a boundary that does not correspond to a real difference in
capability.

404 vs 403 is therefore whatever `require_broker_access` already does,
unchanged: **404** for a broker id that does not exist, **403** for one
that exists but which the caller holds no `BrokerGrant` for. An order
that exists but belongs to a different broker 404s from the detail route,
because every query below is filtered on `broker_id` as well as on the
row's own id - an order from a broker the caller was never authorized for
is simply not in the collection they are addressing, and answering
anything other than "not here" would turn the route into an existence
oracle.

Pagination is `limit` (default 50, max 500) + `offset`, in an
`{items, limit, offset}` envelope - the same convention as D027's
portfolio history, D031's admin listings and D034's broker discovery.
Both listings are ordered NEWEST FIRST (a blotter reads that way),
tie-broken on `id`, unlike D027's history which is oldest-first because
it is an equity curve.
"""

import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.dependencies import AuthorizedBroker, require_broker_access
from apps.api.app.api.schemas_orders import (
    FillBlotterEntry,
    FillEntry,
    ListFillsResponse,
    ListOrdersResponse,
    OrderResponse,
)
from apps.api.app.auth.permissions import Permission
from apps.api.app.db.base import get_session
from apps.api.app.db.models import BrokerKind
from apps.api.app.db.models import Fill as FillRow
from apps.api.app.db.models import Order as OrderRow

router = APIRouter(prefix="/brokers/{broker_id}/orders", tags=["orders"])
fills_router = APIRouter(prefix="/brokers/{broker_id}/fills", tags=["orders"])

DEFAULT_LIST_LIMIT = 50
"""Same default page size as D027's portfolio history, D031's admin
listings and D034's broker discovery - deliberately one convention, not a
fourth."""
MAX_LIST_LIMIT = 500
"""Same hard ceiling, same reasoning: a caller wanting more pages rather
than the server ever building an unbounded response. `limit=501` is a 422
from FastAPI's own validation, never a silently clamped page."""


async def _fills_by_order(
    session: AsyncSession, order_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[FillRow]]:
    """The fills for a page of orders, in one indexed query rather than
    one per row.

    Done as an explicit second SELECT instead of adding an
    `Order.fills` relationship to db/models.py: this phase is additive and
    read-only, and a lazy-loading relationship on the ORM class the write
    path constructs is exactly the kind of change that can surface as a
    `MissingGreenlet` somewhere in `submit_trade_and_record()` rather than
    here. The cost is one extra round trip per page.
    """
    if not order_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(FillRow)
                .where(FillRow.order_id.in_(order_ids))
                .order_by(FillRow.filled_at.asc(), FillRow.id.asc())
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[FillRow]] = defaultdict(list)
    for row in rows:
        grouped[row.order_id].append(row)
    return grouped


def _to_fill_entry(row: FillRow) -> FillEntry:
    return FillEntry(
        id=row.id,
        order_id=row.order_id,
        quantity=row.quantity,
        fill_price=row.fill_price,
        filled_at=row.filled_at,
    )


def _to_order_response(
    row: OrderRow, *, broker_kind: BrokerKind, fills: list[FillRow]
) -> OrderResponse:
    """One `orders` row -> its response shape, shared by the listing and
    the single-order detail route so the two can never disagree about the
    same order (the same reason routes/portfolio.py shares
    `_to_history_entry` between its POST and its history listing).

    `broker_kind` is passed in rather than looked up per row: every route
    here is broker-scoped, so `require_broker_access` has already loaded
    the one `Broker` row that all of these orders belong to. It is the
    joined value from `brokers.kind`, never an inference - `orders` has no
    paper/live column of its own (D065).
    """
    return OrderResponse(
        id=row.id,
        broker_id=row.broker_id,
        broker_kind=broker_kind,
        symbol=row.symbol,
        side=row.side,
        quantity=row.quantity,
        estimated_price=row.estimated_price,
        stop_price=row.stop_price,
        status=row.status,
        risk_block_reason=row.risk_block_reason,
        risk_detail=row.risk_detail,
        portfolio_action=row.portfolio_action,
        portfolio_binding_constraint=row.portfolio_binding_constraint,
        portfolio_detail=row.portfolio_detail,
        portfolio_requested_quantity=row.portfolio_requested_quantity,
        submitted_by_user_id=row.submitted_by_user_id,
        submitted_at=row.submitted_at,
        fills=[_to_fill_entry(fill) for fill in fills],
    )


@router.get("", response_model=ListOrdersResponse)
async def list_orders_endpoint(
    broker_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    authorized: AuthorizedBroker = Depends(require_broker_access(Permission.VIEW_PORTFOLIO)),
) -> ListOrdersResponse:
    """Every order this broker recorded, newest first, paginated.

    REJECTED orders are included and are not a defect: an order the Risk
    Engine or the Portfolio Manager refused is precisely the part of the
    audit trail that shows the controls working, and hiding it would make
    this listing a record of successes rather than of decisions (spec
    Sec17). A client wanting only executions should read `.../fills`,
    which is the other endpoint in this module.

    Ordered by `submitted_at DESC, id DESC`. The id tiebreaker matters:
    `submitted_at` is a server-side `now()`, so two orders written inside
    one transaction can share a timestamp exactly, and without a total
    order `offset` paging could skip or repeat a row.
    """
    rows = (
        (
            await session.execute(
                select(OrderRow)
                .where(OrderRow.broker_id == broker_id)
                .order_by(OrderRow.submitted_at.desc(), OrderRow.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    fills = await _fills_by_order(session, [row.id for row in rows])
    return ListOrdersResponse(
        orders=[
            _to_order_response(
                row, broker_kind=authorized.broker.kind, fills=fills.get(row.id, [])
            )
            for row in rows
        ],
        limit=limit,
        offset=offset,
    )


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order_endpoint(
    broker_id: uuid.UUID,
    order_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    authorized: AuthorizedBroker = Depends(require_broker_access(Permission.VIEW_PORTFOLIO)),
) -> OrderResponse:
    """One order and its fills - 0 for a rejected order, 1 for a filled
    one, and a list rather than a single object so partial fills need no
    response-shape change when the schema gains them (see `Fill`).

    The lookup is filtered on `broker_id` as well as `order_id`, so a real
    order id belonging to a broker the caller is not addressing returns
    **404**, identically to an id that does not exist at all. That is
    deliberate: distinguishing the two would let a caller with a grant on
    any one broker probe for order ids on every other.
    """
    row = (
        await session.execute(
            select(OrderRow).where(OrderRow.id == order_id, OrderRow.broker_id == broker_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"No order with id {order_id} for broker {broker_id}."
        )

    fills = await _fills_by_order(session, [row.id])
    return _to_order_response(
        row, broker_kind=authorized.broker.kind, fills=fills.get(row.id, [])
    )


@fills_router.get("", response_model=ListFillsResponse)
async def list_fills_endpoint(
    broker_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    authorized: AuthorizedBroker = Depends(require_broker_access(Permission.VIEW_PORTFOLIO)),
) -> ListFillsResponse:
    """A flat blotter: one row per actual execution, newest first.

    This is not the orders listing with the rejections filtered out - it
    is a different grain. A partially-filled order (not reachable today,
    but the reason `fills` is its own table) contributes one row here per
    execution and exactly one row to the orders listing, and only this
    endpoint would show both executions as separate events.

    `fills` carries no `broker_id`, so scoping is a join through
    `orders.broker_id` - the same authorization boundary as everything
    else in this module, enforced in the query rather than after the fact.
    Ordered by `filled_at DESC, id DESC`, with the same id tiebreaker and
    for the same reason as the orders listing.
    """
    rows = (
        await session.execute(
            select(FillRow, OrderRow)
            .join(OrderRow, OrderRow.id == FillRow.order_id)
            .where(OrderRow.broker_id == broker_id)
            .order_by(FillRow.filled_at.desc(), FillRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return ListFillsResponse(
        fills=[
            FillBlotterEntry(
                id=fill.id,
                order_id=fill.order_id,
                quantity=fill.quantity,
                fill_price=fill.fill_price,
                filled_at=fill.filled_at,
                broker_id=order.broker_id,
                broker_kind=authorized.broker.kind,
                symbol=order.symbol,
                side=order.side,
            )
            for fill, order in rows
        ],
        limit=limit,
        offset=offset,
    )
