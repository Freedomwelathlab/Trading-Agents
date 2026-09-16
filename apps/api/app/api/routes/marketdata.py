"""Read-only market data. Also the same router trade submission uses
when a caller omits estimated_price (D017) - this endpoint lets a client
preview the price that submitting without one would use."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.dependencies import get_market_data_router
from apps.api.app.api.schemas_marketdata import BarResponse, BarsResponse, QuoteResponse
from apps.api.app.auth.dependencies import get_current_user
from apps.api.app.db.base import get_session
from apps.api.app.db.models import User
from apps.api.app.marketdata.bar_provider import BarInterval
from apps.api.app.marketdata.resolution import resolve_quote
from apps.api.app.marketdata.router import MarketDataRouter
from apps.api.app.marketdata.store import MarketDataStore

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


MAX_BARS = 5_000
"""Ceiling on one response. Intraday makes this matter in a way daily never
did: a year of 5m bars is roughly 105,000 rows, which is neither a useful
chart nor a reasonable payload. The cap REFUSES with a 422 rather than
silently truncating - a truncated series would draw a chart that looks
complete while ending in the middle of the requested window, and a reader
would have no way to tell."""


@router.get("/{symbol}/bars", response_model=BarsResponse)
async def get_bars(
    symbol: str,
    start_date: date = Query(...),
    end_date: date = Query(...),
    bar_interval: BarInterval = Query("1d"),
    session: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
) -> BarsResponse:
    """Persisted OHLCV bars for one symbol and interval over a window.

    Reads `market_data_bars` through `MarketDataStore` - the same reader
    `engine_v2` backtests against - so a chart drawn from this endpoint
    shows exactly the bars a backtest replayed, never a second fetch from a
    vendor that could disagree with them. That identity is the whole point
    of the endpoint: a signal marker is only meaningful if it sits on the
    bar the engine actually saw.

    **Never contacts a vendor and never ingests.** An un-backfilled symbol
    returns 200 with an empty list, not a 404 and not a silent backfill -
    reading is a read. Ingestion stays an explicit, ADMIN-gated action
    (`POST /admin/market-data/backfill`), so a chart request can never spend
    a vendor quota or write rows.

    Requires only an authenticated user, matching `GET /{symbol}/quote`
    beside it: this returns historical market data the account already
    holds, which is not privileged relative to a live quote.
    """
    if end_date < start_date:
        raise HTTPException(
            status_code=422,
            detail=f"end_date {end_date.isoformat()} is before start_date "
            f"{start_date.isoformat()}.",
        )

    bars = await MarketDataStore(session).get_bars(
        symbol, bar_interval=bar_interval, start_date=start_date, end_date=end_date
    )
    if len(bars) > MAX_BARS:
        raise HTTPException(
            status_code=422,
            detail=f"{len(bars)} {bar_interval} bars for {symbol} between "
            f"{start_date.isoformat()} and {end_date.isoformat()} exceeds the "
            f"{MAX_BARS}-bar response cap. Narrow the window or use a coarser interval.",
        )

    return BarsResponse(
        symbol=symbol,
        bar_interval=bar_interval,
        count=len(bars),
        bars=[
            BarResponse(
                ts=bar.ts,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                source=bar.source,
            )
            for bar in bars
        ],
    )
