"""Read-only market data. Deliberately not wired into trade submission
yet - whether a proposal's price should come from a live quote or stay
caller-supplied is a separate, not-yet-made decision (see
docs/DECISIONS.md D015's Consequences)."""

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
