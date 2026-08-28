"""Read-only portfolio reporting (D022). Never moves money or state -
purely reads broker_accounts/broker_positions/orders/fills and computes a
snapshot via apps/api/app/portfolio/snapshot.py. Gated by
Permission.VIEW_PORTFOLIO (not SUBMIT_PAPER_TRADE - see that permission's
docstring for why) plus the same require_broker_access grant check every
other broker-scoped route uses.

`marks` travels as a JSON request body on a GET, same dict[symbol, price]
shape as TradeSubmissionRequest.marks - a query string can't cleanly carry
an arbitrary symbol->price mapping, and this endpoint has no other use for
a body. A missing mark for a currently-held symbol is a 400
DATA_UNAVAILABLE, never a guessed price (spec Sec57), exactly like the
trades endpoint.
"""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.dependencies import AuthorizedBroker, require_broker_access
from apps.api.app.auth.permissions import Permission
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.db.base import get_session
from apps.api.app.portfolio.errors import BrokerAccountNotFoundError, MissingMarkError
from apps.api.app.portfolio.models import PortfolioSnapshot
from apps.api.app.portfolio.snapshot import compute_portfolio_snapshot

router = APIRouter(prefix="/brokers/{broker_id}/portfolio", tags=["portfolio"])


class PortfolioMarksRequest(BaseModel):
    marks: dict[str, Decimal] = Field(default_factory=dict)
    """Current price for every symbol this broker currently holds - same
    contract as TradeSubmissionRequest.marks. Omitting a held symbol's
    mark fails the request rather than guessing a stale price."""


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
