"""Read-only market data. Also the same router trade submission uses
when a caller omits estimated_price (D017) - this endpoint lets a client
preview the price that submitting without one would use."""

from fastapi import APIRouter, Depends, HTTPException

from apps.api.app.api.dependencies import get_market_data_router
from apps.api.app.api.schemas_marketdata import QuoteResponse
from apps.api.app.auth.dependencies import get_current_user
from apps.api.app.db.models import User
from apps.api.app.marketdata.resolution import resolve_quote
from apps.api.app.marketdata.router import MarketDataRouter

router = APIRouter(prefix="/market-data", tags=["market-data"])


@router.get("/{symbol}/quote", response_model=QuoteResponse)
async def get_quote(
    symbol: str,
    market_data_router: MarketDataRouter | None = Depends(get_market_data_router),
    _current_user: User = Depends(get_current_user),
) -> QuoteResponse:
    """Unchanged wire contract: 503 NOT_CONFIGURED when no vendor is
    wired, 404 NO_DATA_AVAILABLE when the wired vendors have no quote,
    200 otherwise. As of Phase 50 the decision itself lives in
    `apps.api.app.marketdata.resolution` so `GET /watchlists/{id}/quotes`
    resolves each of its symbols through this exact same path rather than
    a second copy of it."""
    resolved = await resolve_quote(market_data_router, symbol)

    if resolved.not_configured:
        raise HTTPException(status_code=503, detail=resolved.unavailable)
    if resolved.snapshot is None:
        raise HTTPException(status_code=404, detail=resolved.unavailable)

    snapshot = resolved.snapshot
    return QuoteResponse(
        symbol=snapshot.symbol, price=snapshot.price, as_of=snapshot.as_of, source=snapshot.source
    )
