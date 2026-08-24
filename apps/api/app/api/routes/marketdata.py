"""Read-only market data. Also the same router trade submission uses
when a caller omits estimated_price (D017) - this endpoint lets a client
preview the price that submitting without one would use."""

from fastapi import APIRouter, Depends, HTTPException

from apps.api.app.api.dependencies import get_market_data_router
from apps.api.app.api.schemas_marketdata import QuoteResponse
from apps.api.app.auth.dependencies import get_current_user
from apps.api.app.db.models import User
from apps.api.app.marketdata.router import MarketDataRouter, NoDataAvailableError

router = APIRouter(prefix="/market-data", tags=["market-data"])


@router.get("/{symbol}/quote", response_model=QuoteResponse)
async def get_quote(
    symbol: str,
    market_data_router: MarketDataRouter | None = Depends(get_market_data_router),
    _current_user: User = Depends(get_current_user),
) -> QuoteResponse:
    if market_data_router is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "NOT_CONFIGURED: no market data vendor is wired "
                "(see docs/DECISIONS.md D008/D015)."
            ),
        )

    try:
        snapshot = await market_data_router.get_snapshot(symbol)
    except NoDataAvailableError as exc:
        # Message is already NO_DATA_AVAILABLE:-prefixed by the router.
        raise HTTPException(status_code=404, detail=str(exc)) from None

    return QuoteResponse(
        symbol=snapshot.symbol, price=snapshot.price, as_of=snapshot.as_of, source=snapshot.source
    )
