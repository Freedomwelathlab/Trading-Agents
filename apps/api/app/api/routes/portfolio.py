"""Read-only portfolio reporting (D022) plus persisted snapshot history
(D027). Never moves money or state - purely reads
broker_accounts/broker_positions/orders/fills and computes a snapshot via
apps/api/app/portfolio/snapshot.py; the POST endpoint additionally
persists that computed snapshot as an append-only row (never a fabricated
or scheduled one - see D027). All three routes are gated by
Permission.VIEW_PORTFOLIO (not SUBMIT_PAPER_TRADE - see that permission's
docstring, and D027, for why creating a persisted record of the current
state is still a "view" capability) plus the same require_broker_access
grant check every other broker-scoped route uses.

`marks` travels as a JSON request body on GET/POST, same dict[symbol,
price] shape as TradeSubmissionRequest.marks - a query string can't
cleanly carry an arbitrary symbol->price mapping, and neither endpoint
has any other use for a body. A missing mark for a currently-held symbol
is a 400 DATA_UNAVAILABLE, never a guessed price (spec Sec57), exactly
like the trades endpoint.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.api.app.api.dependencies import AuthorizedBroker, require_broker_access
from apps.api.app.auth.permissions import Permission
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.db.base import get_session
from apps.api.app.db.models import PortfolioSnapshotPositionRow, PortfolioSnapshotRow
from apps.api.app.portfolio.errors import BrokerAccountNotFoundError, MissingMarkError
from apps.api.app.portfolio.models import PortfolioPosition, PortfolioSnapshot
from apps.api.app.portfolio.snapshot import compute_portfolio_snapshot

router = APIRouter(prefix="/brokers/{broker_id}/portfolio", tags=["portfolio"])

DEFAULT_HISTORY_LIMIT = 50
"""Reasonable default page size for GET .../history - enough to cover a
few weeks of a few-times-a-day manual snapshot cadence without the
default request pulling an unbounded amount of history (docs/DECISIONS.md
D027)."""
MAX_HISTORY_LIMIT = 500
"""Hard ceiling on `limit` regardless of what the caller requests - this
is a manual, caller-triggered snapshot system (D027), not a
high-frequency time series, so 500 rows is already generous; a caller
needing more should page with `offset` rather than the server building
one unbounded response."""


class PortfolioMarksRequest(BaseModel):
    marks: dict[str, Decimal] = Field(default_factory=dict)
    """Current price for every symbol this broker currently holds - same
    contract as TradeSubmissionRequest.marks. Omitting a held symbol's
    mark fails the request rather than guessing a stale price."""


class PortfolioSnapshotHistoryEntry(BaseModel):
    id: uuid.UUID
    broker_id: uuid.UUID
    captured_at: datetime
    cash: Decimal
    positions: list[PortfolioPosition]
    total_equity: Decimal
    total_unrealized_pnl: Decimal
    total_realized_pnl: Decimal


class PortfolioSnapshotHistoryResponse(BaseModel):
    snapshots: list[PortfolioSnapshotHistoryEntry]
    limit: int
    offset: int


@router.get("", response_model=PortfolioSnapshot)
async def get_portfolio_endpoint(
    broker_id: uuid.UUID,
    body: PortfolioMarksRequest = Body(default_factory=PortfolioMarksRequest),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    authorized: AuthorizedBroker = Depends(require_broker_access(Permission.VIEW_PORTFOLIO)),
) -> PortfolioSnapshot:
    del authorized  # required for the auth+grant check only; unused otherwise

    try:
        return await compute_portfolio_snapshot(
            broker_id,
            session,
            marks=body.marks,
            default_starting_cash=settings.paper_broker_starting_cash,
        )
    except MissingMarkError as exc:
        raise HTTPException(status_code=400, detail=f"DATA_UNAVAILABLE: {exc}") from None
    except BrokerAccountNotFoundError as exc:
        raise HTTPException(status_code=400, detail=f"DATA_UNAVAILABLE: {exc}") from None


@router.post("/snapshots", response_model=PortfolioSnapshotHistoryEntry, status_code=201)
async def create_portfolio_snapshot_endpoint(
    broker_id: uuid.UUID,
    body: PortfolioMarksRequest = Body(default_factory=PortfolioMarksRequest),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    authorized: AuthorizedBroker = Depends(require_broker_access(Permission.VIEW_PORTFOLIO)),
) -> PortfolioSnapshotHistoryEntry:
    """Computes a snapshot exactly as GET .../portfolio does, then persists
    it as a new append-only row (docs/DECISIONS.md D027). Never a
    scheduled/automatic job - only ever created when a caller explicitly
    POSTs here, and only ever from compute_portfolio_snapshot()'s real
    output (same MissingMarkError/BrokerAccountNotFoundError -> 400
    DATA_UNAVAILABLE discipline as the read endpoint - a snapshot is never
    persisted from a guessed or partial valuation)."""
    del authorized  # required for the auth+grant check only; unused otherwise

    try:
        computed = await compute_portfolio_snapshot(
            broker_id,
            session,
            marks=body.marks,
            default_starting_cash=settings.paper_broker_starting_cash,
        )
    except MissingMarkError as exc:
        raise HTTPException(status_code=400, detail=f"DATA_UNAVAILABLE: {exc}") from None
    except BrokerAccountNotFoundError as exc:
        raise HTTPException(status_code=400, detail=f"DATA_UNAVAILABLE: {exc}") from None

    row = PortfolioSnapshotRow(
        broker_id=broker_id,
        cash=computed.cash,
        total_equity=computed.total_equity,
        total_unrealized_pnl=computed.total_unrealized_pnl,
        total_realized_pnl=computed.total_realized_pnl,
    )
    row.positions = [
        PortfolioSnapshotPositionRow(
            symbol=position.symbol,
            quantity=position.quantity,
            avg_cost=position.avg_cost,
            current_value=position.current_value,
            unrealized_pnl=position.unrealized_pnl,
            realized_pnl=position.realized_pnl,
        )
        for position in computed.positions
    ]
    session.add(row)
    await session.commit()
    await session.refresh(row, attribute_names=["positions"])

    return PortfolioSnapshotHistoryEntry(
        id=row.id,
        broker_id=row.broker_id,
        captured_at=row.captured_at,
        cash=row.cash,
        positions=[
            PortfolioPosition(
                symbol=p.symbol,
                quantity=p.quantity,
                avg_cost=p.avg_cost,
                current_value=p.current_value,
                unrealized_pnl=p.unrealized_pnl,
                realized_pnl=p.realized_pnl,
            )
            for p in row.positions
        ],
        total_equity=row.total_equity,
        total_unrealized_pnl=row.total_unrealized_pnl,
        total_realized_pnl=row.total_realized_pnl,
    )


@router.get("/history", response_model=PortfolioSnapshotHistoryResponse)
async def get_portfolio_history_endpoint(
    broker_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_HISTORY_LIMIT, ge=1, le=MAX_HISTORY_LIMIT),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    authorized: AuthorizedBroker = Depends(require_broker_access(Permission.VIEW_PORTFOLIO)),
) -> PortfolioSnapshotHistoryResponse:
    """Returns persisted snapshots for a broker, oldest-to-newest by
    `captured_at` (docs/DECISIONS.md D027), paginated via `limit`
    (default 50, max 500) and `offset` (default 0) - never the full,
    unbounded history in one response."""
    del authorized  # required for the auth+grant check only; unused otherwise

    rows = (
        (
            await session.execute(
                select(PortfolioSnapshotRow)
                .where(PortfolioSnapshotRow.broker_id == broker_id)
                .options(selectinload(PortfolioSnapshotRow.positions))
                .order_by(PortfolioSnapshotRow.captured_at.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    return PortfolioSnapshotHistoryResponse(
        snapshots=[
            PortfolioSnapshotHistoryEntry(
                id=row.id,
                broker_id=row.broker_id,
                captured_at=row.captured_at,
                cash=row.cash,
                positions=[
                    PortfolioPosition(
                        symbol=p.symbol,
                        quantity=p.quantity,
                        avg_cost=p.avg_cost,
                        current_value=p.current_value,
                        unrealized_pnl=p.unrealized_pnl,
                        realized_pnl=p.realized_pnl,
                    )
                    for p in row.positions
                ],
                total_equity=row.total_equity,
                total_unrealized_pnl=row.total_unrealized_pnl,
                total_realized_pnl=row.total_realized_pnl,
            )
            for row in rows
        ],
        limit=limit,
        offset=offset,
    )
