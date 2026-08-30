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
from apps.api.app.db.models import PortfolioSnapshotRow
from apps.api.app.portfolio.errors import BrokerAccountNotFoundError, MissingMarkError
from apps.api.app.portfolio.models import (
    CostBasisMethod,
    PortfolioPosition,
    PortfolioSnapshot,
)
from apps.api.app.portfolio.persistence import persist_portfolio_snapshot
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


def _to_history_entry(row: PortfolioSnapshotRow) -> PortfolioSnapshotHistoryEntry:
    """One persisted row -> its response shape. Shared by the POST and the
    history listing so a snapshot reads back identically however it was
    captured - including one written by the Phase 27 scheduler
    (apps/api/app/portfolio/scheduler.py), which is deliberately
    indistinguishable here: a snapshot is a snapshot, and both paths run
    the same compute + persist code."""
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


@router.get("", response_model=PortfolioSnapshot)
async def get_portfolio_endpoint(
    broker_id: uuid.UUID,
    body: PortfolioMarksRequest = Body(default_factory=PortfolioMarksRequest),
    cost_basis_method: CostBasisMethod = Query(default=CostBasisMethod.AVERAGE),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    authorized: AuthorizedBroker = Depends(require_broker_access(Permission.VIEW_PORTFOLIO)),
) -> PortfolioSnapshot:
    """`?cost_basis_method=` selects average (default), fifo or lifo for the
    basis-derived figures (D041). Omitting it reproduces the D022 response
    exactly; an unrecognised value is a 422 rather than a silent fallback.
    The same fill history legitimately yields three different realized-P&L
    numbers - that is the point of the parameter, not an inconsistency.

    This read-only endpoint is the only place the choice is offered.
    Persisted snapshots (D027) and the scheduler (D030) stay average-only
    on purpose: `portfolio_snapshots` has no column recording which method
    produced a row, so a stored FIFO snapshot would be indistinguishable
    from an average one in GET .../history - a real misreporting hazard,
    and one that needs a migration to fix rather than a query param."""
    del authorized  # required for the auth+grant check only; unused otherwise

    try:
        return await compute_portfolio_snapshot(
            broker_id,
            session,
            marks=body.marks,
            default_starting_cash=settings.paper_broker_starting_cash,
            cost_basis_method=cost_basis_method,
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
    it as a new append-only row (docs/DECISIONS.md D027). This is the
    caller-triggered path: a row appears here only because someone POSTed,
    with marks they supplied. Phase 27 (D030) added a second, opt-in path -
    apps/api/app/portfolio/scheduler.py, which sources its marks from the
    real MarketDataRouter instead of a request body and skips rather than
    guessing when a quote is unavailable. Both paths run the identical
    compute + persist code, so the two are indistinguishable in the stored
    history by design.

    Same MissingMarkError/BrokerAccountNotFoundError -> 400
    DATA_UNAVAILABLE discipline as the read endpoint - a snapshot is never
    persisted from a guessed or partial valuation."""
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

    # Phase 27 (D030) moved the row-building into
    # apps/api/app/portfolio/persistence.py so this route and the scheduled
    # capture write identical rows through one code path.
    row = await persist_portfolio_snapshot(session, broker_id, computed)
    return _to_history_entry(row)


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
        snapshots=[_to_history_entry(row) for row in rows],
        limit=limit,
        offset=offset,
    )
