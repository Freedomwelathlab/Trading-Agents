"""Paper option routing over HTTP (Phase 102, D122).

    POST /brokers/{broker_id}/option-orders               open or close   trade:submit:paper
    GET  /brokers/{broker_id}/option-orders               order history   portfolio:view
    GET  /brokers/{broker_id}/option-positions            structures + per-contract
                                                          positions, marked   portfolio:view
    POST /brokers/{broker_id}/option-positions/settle     settle expired  trade:submit:paper

All four need the permission AND a `BrokerGrant` on the broker, exactly
like the equity trade routes (`require_broker_access`).

**Paper only.** A `kind=live` broker is refused with
`LIVE_NOT_SUPPORTED` before any quote is read or any row written. There is
no live option order path in this system (D122).

**Every fill is labelled.** The response carries the chain's `source` and
`as_of`, whether that source is delayed, and `pricing_basis` spelling out
that the fill is the delayed mid moved k half-spreads against the trade -
a modelled paper fill, not an execution.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.dependencies import (
    AuthorizedBroker,
    get_option_chain_provider,
    require_broker_access,
)
from apps.api.app.auth.permissions import Permission
from apps.api.app.core.config import get_settings
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_session
from apps.api.app.db.models import BrokerKind, OptionFill, OptionOrder
from apps.api.app.marketdata.option_chain_provider import OptionChainProvider
from apps.api.app.options.paper_book import (
    OpenOrderRequest,
    OptionOrderOutcome,
    book_view,
    is_delayed,
    settle_expired,
    submit_close,
    submit_open,
    utc_now,
)
from apps.api.app.options.paper_orders import (
    LegSide,
    LegSpec,
    OptionOrderError,
    OptionStructureType,
)
from apps.api.app.options.pricing import OptionRight

router = APIRouter(prefix="/brokers/{broker_id}", tags=["options", "trades"])
logger = get_logger(__name__)

PRICING_BASIS = (
    "MODELLED paper fill: the option chain's mid moved k x half-spread against the trade "
    "(buy at mid + k*half-spread, sell at mid - k*half-spread), from the quote source and "
    "timestamp shown. Not an execution at any venue."
)


def option_error_status(code: str) -> int:
    return {
        "NOT_CONFIGURED": 503,
        "VENDOR_ERROR": 502,
        "DATA_UNAVAILABLE": 409,
        "NO_SUCH_STRUCTURE": 404,
        "NOT_OPEN": 409,
    }.get(code, 400)


def _http(exc: OptionOrderError) -> HTTPException:
    code = str(exc).split(":", 1)[0]
    return HTTPException(status_code=option_error_status(code), detail=str(exc))


def _refuse_live(authorized: AuthorizedBroker) -> None:
    if authorized.broker.kind is not BrokerKind.PAPER:
        raise HTTPException(
            status_code=400,
            detail=(
                "LIVE_NOT_SUPPORTED: option orders are PAPER ONLY. This broker is a live "
                "broker; no live option order path exists in this system, so nothing was "
                "priced, recorded or sent (docs/DECISIONS.md D122)."
            ),
        )


# --- schemas -----------------------------------------------------------------


class LegIn(BaseModel):
    right: OptionRight
    strike: Decimal = Field(gt=0)
    side: LegSide


class OptionOrderRequest(BaseModel):
    action: Literal["open", "close"] = "open"
    underlying: str | None = Field(default=None, min_length=1, max_length=32)
    expiry: date | None = None
    structure: OptionStructureType | None = None
    legs: list[LegIn] = Field(default_factory=list, max_length=4)
    quantity: int | None = Field(default=None, ge=1, le=1000)
    structure_id: uuid.UUID | None = None
    """For `action: close` - the open position to close, whole."""


class FillOut(BaseModel):
    contract_symbol: str
    right: str
    strike: Decimal
    side: str
    quantity: int
    bid: Decimal
    ask: Decimal
    mid: Decimal
    fill_price: Decimal
    quote_source: str
    quote_as_of: datetime


class OptionOrderResponse(BaseModel):
    order_id: uuid.UUID
    action: str
    status: str
    approved: bool
    block_reason: str | None
    detail: str | None
    structure_id: uuid.UUID | None
    structure_type: str
    underlying: str
    expiry: date
    quantity: int
    net_price: Decimal | None
    """Per share, signed from the account's view of THIS order: + paid, - received."""
    cash_change: Decimal | None
    max_loss: Decimal | None
    max_profit: Decimal | None
    realized_pnl: Decimal | None
    quote_source: str
    quote_as_of: datetime
    quote_delayed: bool
    fill_haircut_k: Decimal
    pricing_basis: str
    fills: list[FillOut]


def _fill_out(f: OptionFill) -> FillOut:
    return FillOut(
        contract_symbol=f.contract_symbol, right=f.right, strike=f.strike, side=f.side,
        quantity=f.quantity, bid=f.bid, ask=f.ask, mid=f.mid, fill_price=f.fill_price,
        quote_source=f.quote_source, quote_as_of=f.quote_as_of,
    )


def _order_out(outcome: OptionOrderOutcome) -> OptionOrderResponse:
    o = outcome.order
    s = outcome.structure
    econ = outcome.economics
    max_loss = s.max_loss if s is not None else (
        econ.max_loss_per_contract * o.quantity if econ is not None else None
    )
    max_profit = s.max_profit if s is not None else (
        econ.max_profit_per_contract * o.quantity
        if econ is not None and econ.max_profit_per_contract is not None
        else None
    )
    return OptionOrderResponse(
        order_id=o.id, action=o.action, status=o.status.value, approved=outcome.filled,
        block_reason=o.block_reason, detail=o.detail, structure_id=o.structure_id,
        structure_type=o.structure_type, underlying=o.underlying, expiry=o.expiry,
        quantity=o.quantity, net_price=o.net_price, cash_change=o.cash_change,
        max_loss=max_loss, max_profit=max_profit,
        realized_pnl=s.realized_pnl if (s is not None and o.action == "close") else None,
        quote_source=o.quote_source, quote_as_of=o.quote_as_of,
        quote_delayed=is_delayed(o.quote_source), fill_haircut_k=o.fill_haircut_k,
        pricing_basis=PRICING_BASIS, fills=[_fill_out(f) for f in outcome.fills],
    )


class StructureOut(BaseModel):
    id: uuid.UUID
    options_bot_id: uuid.UUID | None
    structure_type: str
    underlying: str
    expiry: date
    quantity: int
    legs: list[dict]
    entry_net_price: Decimal
    max_loss: Decimal
    max_profit: Decimal | None
    capital_reserved: Decimal
    opened_at: datetime
    entry_quote_source: str
    entry_quote_as_of: datetime
    mid_value: Decimal | None
    close_value: Decimal | None
    unrealized_pnl_mid: Decimal | None
    unrealized_pnl_close: Decimal | None
    mark_source: str | None
    mark_as_of: datetime | None
    mark_delayed: bool | None
    note: str | None


class ContractPositionOut(BaseModel):
    contract_symbol: str
    underlying: str
    expiry: date
    right: str
    strike: Decimal
    quantity: int
    avg_entry_price: Decimal
    bid: Decimal | None
    ask: Decimal | None
    mid: Decimal | None
    quote_source: str | None
    quote_as_of: datetime | None


class OptionBookResponse(BaseModel):
    broker_id: uuid.UUID
    cash: Decimal | None
    capital_reserved: Decimal
    equity_at_cost: Decimal | None
    """cash + capital reserved by open structures - option positions at
    their entry capital, not marked. Equity positions not included."""
    structures: list[StructureOut]
    positions: list[ContractPositionOut]
    marks_basis: str


class SettlementOut(BaseModel):
    structure_id: uuid.UUID
    settled: bool
    detail: str
    realized_pnl: Decimal | None
    settlement_underlying_price: Decimal | None


class SettleResponse(BaseModel):
    results: list[SettlementOut]


class OrderHistoryRow(BaseModel):
    order_id: uuid.UUID
    action: str
    status: str
    block_reason: str | None
    detail: str | None
    structure_id: uuid.UUID | None
    structure_type: str
    underlying: str
    expiry: date
    quantity: int
    net_price: Decimal | None
    cash_change: Decimal | None
    quote_source: str
    quote_as_of: datetime
    fill_haircut_k: Decimal
    options_bot_run_id: uuid.UUID | None
    created_at: datetime
    fills: list[FillOut]


class OrderHistoryResponse(BaseModel):
    orders: list[OrderHistoryRow]
    limit: int
    offset: int


# --- routes ------------------------------------------------------------------


@router.post("/option-orders", response_model=OptionOrderResponse)
async def submit_option_order(
    payload: OptionOrderRequest,
    request: Request,
    authorized: AuthorizedBroker = Depends(require_broker_access(Permission.SUBMIT_PAPER_TRADE)),
    provider: OptionChainProvider | None = Depends(get_option_chain_provider),
    session: AsyncSession = Depends(get_session),
) -> OptionOrderResponse:
    _refuse_live(authorized)
    settings = get_settings()
    now = utc_now()
    bar_router = getattr(request.app.state, "market_data_bar_backfill_provider", None)
    try:
        # Anything already past expiry settles first, so this order is
        # sized against the cash that settlement returns.
        await settle_expired(
            session, broker_id=authorized.broker.id, settings=settings, now=now,
            bar_router=bar_router,
        )
        if payload.action == "close":
            if payload.structure_id is None:
                raise OptionOrderError("BAD_REQUEST: a close needs structure_id.")
            outcome = await submit_close(
                session, broker=authorized.broker, structure_id=payload.structure_id,
                provider=provider, settings=settings, now=now,
                submitted_by_user_id=authorized.user.id, reason="operator",
            )
        else:
            if (
                payload.underlying is None or payload.expiry is None
                or payload.structure is None or payload.quantity is None
            ):
                raise OptionOrderError(
                    "BAD_REQUEST: an open needs underlying, expiry, structure, legs and quantity."
                )
            outcome = await submit_open(
                session,
                broker=authorized.broker,
                request=OpenOrderRequest(
                    underlying=payload.underlying.strip().upper(),
                    expiry=payload.expiry,
                    structure_type=payload.structure,
                    legs=tuple(
                        LegSpec(right=leg.right, strike=leg.strike, side=leg.side)
                        for leg in payload.legs
                    ),
                    quantity=payload.quantity,
                ),
                provider=provider, settings=settings, now=now,
                submitted_by_user_id=authorized.user.id,
            )
    except OptionOrderError as exc:
        await session.rollback()
        raise _http(exc) from None
    await session.commit()
    logger.info(
        "option_order_decided",
        broker_id=str(authorized.broker.id), action=outcome.order.action,
        status=outcome.order.status.value, block_reason=outcome.order.block_reason,
        structure_type=outcome.order.structure_type, quote_source=outcome.order.quote_source,
    )
    return _order_out(outcome)


@router.get("/option-orders", response_model=OrderHistoryResponse)
async def list_option_orders(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    authorized: AuthorizedBroker = Depends(require_broker_access(Permission.VIEW_PORTFOLIO)),
    session: AsyncSession = Depends(get_session),
) -> OrderHistoryResponse:
    rows = (
        await session.execute(
            select(OptionOrder)
            .where(OptionOrder.broker_id == authorized.broker.id)
            .order_by(OptionOrder.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    fills_by_order: dict[uuid.UUID, list[OptionFill]] = {}
    if rows:
        fills = (
            await session.execute(
                select(OptionFill).where(OptionFill.order_id.in_([r.id for r in rows]))
            )
        ).scalars().all()
        for f in fills:
            fills_by_order.setdefault(f.order_id, []).append(f)
    return OrderHistoryResponse(
        orders=[
            OrderHistoryRow(
                order_id=r.id, action=r.action, status=r.status.value,
                block_reason=r.block_reason, detail=r.detail, structure_id=r.structure_id,
                structure_type=r.structure_type, underlying=r.underlying, expiry=r.expiry,
                quantity=r.quantity, net_price=r.net_price, cash_change=r.cash_change,
                quote_source=r.quote_source, quote_as_of=r.quote_as_of,
                fill_haircut_k=r.fill_haircut_k, options_bot_run_id=r.options_bot_run_id,
                created_at=r.created_at,
                fills=[_fill_out(f) for f in fills_by_order.get(r.id, [])],
            )
            for r in rows
        ],
        limit=limit,
        offset=offset,
    )


@router.get("/option-positions", response_model=OptionBookResponse)
async def get_option_positions(
    authorized: AuthorizedBroker = Depends(require_broker_access(Permission.VIEW_PORTFOLIO)),
    provider: OptionChainProvider | None = Depends(get_option_chain_provider),
    session: AsyncSession = Depends(get_session),
) -> OptionBookResponse:
    view = await book_view(
        session, authorized.broker.id, provider=provider, now=utc_now(),
        k=get_settings().options_paper_fill_haircut_k,
    )
    return OptionBookResponse(
        broker_id=authorized.broker.id,
        cash=view.cash,
        capital_reserved=view.capital_reserved,
        equity_at_cost=(view.cash + view.capital_reserved) if view.cash is not None else None,
        structures=[
            StructureOut(
                id=m.structure.id, options_bot_id=m.structure.options_bot_id,
                structure_type=m.structure.structure_type, underlying=m.structure.underlying,
                expiry=m.structure.expiry, quantity=m.structure.quantity,
                legs=list(m.structure.legs), entry_net_price=m.structure.entry_net_price,
                max_loss=m.structure.max_loss, max_profit=m.structure.max_profit,
                capital_reserved=m.structure.capital_reserved, opened_at=m.structure.opened_at,
                entry_quote_source=m.structure.quote_source,
                entry_quote_as_of=m.structure.quote_as_of,
                mid_value=m.mid_value, close_value=m.close_value,
                unrealized_pnl_mid=m.unrealized_pnl_mid,
                unrealized_pnl_close=m.unrealized_pnl_close,
                mark_source=m.quote_source, mark_as_of=m.quote_as_of,
                mark_delayed=is_delayed(m.quote_source) if m.quote_source else None,
                note=m.note,
            )
            for m in view.structures
        ],
        positions=[
            ContractPositionOut(
                contract_symbol=p.contract_symbol, underlying=p.underlying, expiry=p.expiry,
                right=p.right, strike=p.strike, quantity=p.quantity,
                avg_entry_price=p.avg_entry_price, bid=p.bid, ask=p.ask, mid=p.mid,
                quote_source=p.quote_source, quote_as_of=p.quote_as_of,
            )
            for p in view.positions
        ],
        marks_basis=(
            "mid_value/unrealized_pnl_mid: the delayed quote mids; close_value/"
            "unrealized_pnl_close: what closing now would fill at under the paper haircut. "
            "Both are per the quote source and as_of shown; an unmarkable leg leaves the "
            "structure unmarked rather than priced at a guess."
        ),
    )


@router.post("/option-positions/settle", response_model=SettleResponse)
async def settle_option_positions(
    request: Request,
    authorized: AuthorizedBroker = Depends(require_broker_access(Permission.SUBMIT_PAPER_TRADE)),
    session: AsyncSession = Depends(get_session),
) -> SettleResponse:
    _refuse_live(authorized)
    results = await settle_expired(
        session, broker_id=authorized.broker.id, settings=get_settings(), now=utc_now(),
        bar_router=getattr(request.app.state, "market_data_bar_backfill_provider", None),
    )
    await session.commit()
    return SettleResponse(
        results=[
            SettlementOut(
                structure_id=r.structure.id, settled=r.settled, detail=r.detail,
                realized_pnl=r.structure.realized_pnl if r.settled else None,
                settlement_underlying_price=(
                    r.structure.settlement_underlying_price if r.settled else None
                ),
            )
            for r in results
        ]
    )
