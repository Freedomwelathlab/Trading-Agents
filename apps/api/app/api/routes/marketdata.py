"""Read-only market data. Also the same router trade submission uses
when a caller omits estimated_price (D017) - this endpoint lets a client
preview the price that submitting without one would use."""

from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.dependencies import get_depth_provider, get_market_data_router
from apps.api.app.api.schemas_marketdata import (
    BarResponse,
    BarsResponse,
    DepthLevelResponse,
    OrderBookResponse,
    QuoteResponse,
    SessionLevelsResponse,
)
from apps.api.app.auth.dependencies import get_current_user
from apps.api.app.db.base import get_session
from apps.api.app.db.models import User
from apps.api.app.marketdata.bar_provider import BarInterval
from apps.api.app.marketdata.depth_provider import DepthProvider
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.resolution import resolve_quote
from apps.api.app.marketdata.router import MarketDataRouter
from apps.api.app.marketdata.sessions import (
    build_session_levels,
    session_date,
    session_vwap,
)
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


@router.get("/{symbol}/depth", response_model=OrderBookResponse)
async def get_depth(
    symbol: str,
    depth_provider: DepthProvider | None = Depends(get_depth_provider),
    _current_user: User = Depends(get_current_user),
) -> OrderBookResponse:
    """The live order book (Phase 74, D092).

    Three distinct answers, and the difference between the last two is the
    point of this endpoint:

      * **503 NOT_CONFIGURED** - no vendor is wired at all.
      * **404 DATA_UNAVAILABLE** - a vendor answered and had no priced
        level. That is the ordinary case for a closed market, and also
        what an entitlement without a depth ladder looks like; the two are
        indistinguishable from one response and this does not pretend
        otherwise.
      * **200** - a real book, best price first.

    A vendor's empty book NEVER becomes a 200 with zero-priced levels.
    Measured on this account: `depth("TQQQ.US")` returns one bid and one
    ask whose price is null and whose volume is zero, while `700.HK`
    returns genuine levels. Rendering the first as `0.00 x 0` would draw a
    tight, empty, entirely fictional book.
    """
    if depth_provider is None:
        raise HTTPException(
            status_code=503,
            detail="NOT_CONFIGURED: no market-data vendor is wired for order-book depth.",
        )

    try:
        book = await depth_provider.get_depth(symbol)
    except DataUnavailableError as exc:
        raise HTTPException(status_code=404, detail=f"DATA_UNAVAILABLE: {exc}") from exc
    except VendorError as exc:
        raise HTTPException(status_code=502, detail=f"VENDOR_ERROR: {exc}") from exc

    return OrderBookResponse(
        symbol=book.symbol,
        bids=[
            DepthLevelResponse(price=lvl.price, volume=lvl.volume, order_count=lvl.order_count)
            for lvl in book.bids
        ],
        asks=[
            DepthLevelResponse(price=lvl.price, volume=lvl.volume, order_count=lvl.order_count)
            for lvl in book.asks
        ],
        spread=book.spread,
        as_of=book.as_of,
        source=book.source,
    )


_PRICE_PRECISION = Decimal("0.000001")
"""Six decimals - the precision `market_data_bars` actually stores
(`NUMERIC(20,6)`).

VWAP and its bands are DERIVED from those prices by division, so they come
out of `Decimal` carrying the context's full 28 significant digits: a
session VWAP renders as `68.26864187763813456219701391`, which is not a
price anybody can act on and reads as false precision about a number whose
inputs have six decimals. Quantizing here rather than formatting in the UI
keeps one definition of "how precise is this figure" on the server, where
the precision of the inputs is actually known.

Levels that come straight from a bar are NOT re-quantized - they are
already exactly what was stored, and rounding a stored price would make
the API disagree with the bar it came from.
"""


def _price(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_PRICE_PRECISION)


@router.get("/{symbol}/session-levels", response_model=SessionLevelsResponse)
async def get_session_levels(
    symbol: str,
    bar_interval: BarInterval = Query("5m"),
    session: AsyncSession = Depends(get_session),
    _current_user: User = Depends(get_current_user),
) -> SessionLevelsResponse:
    """Session-anchored levels for the most recent stored session
    (Phase 74, D092).

    Computed from `market_data_bars` rather than requested from the
    vendor, because these are not quantities a vendor publishes - they are
    derived from where each bar sits on the exchange's own clock. That
    also means this endpoint answers when the market is closed, which is
    exactly when a trader marks levels for the next session.

    **The previous day is the previous session PRESENT IN THE DATA**, not
    the calendar day before, so a Monday reads Friday and a post-holiday
    session reads the last day that traded. 404 when no bars are stored:
    an absent series is a gap to backfill, never a session of nulls.
    """
    store = MarketDataStore(session)
    # Enough history to establish a previous session plus this one, with
    # room for a long weekend. Bounded rather than open-ended: the levels
    # only ever describe the latest session, so reading the whole series
    # would cost more the longer the platform runs.
    end = date.today()
    bars = await store.get_bars(
        symbol, bar_interval=bar_interval, start_date=end - timedelta(days=10), end_date=end
    )
    if not bars:
        raise HTTPException(
            status_code=404,
            detail=(
                f"DATA_UNAVAILABLE: no {bar_interval} bars stored for {symbol!r} in the "
                "last 10 days. Backfill via POST /admin/market-data/backfill first."
            ),
        )

    levels_by_day = build_session_levels(bars)
    latest_day = max(levels_by_day)
    levels = levels_by_day[latest_day]

    session_bars = [b for b in bars if session_date(b.ts) == latest_day]
    vwap_points = session_vwap(session_bars)
    last_vwap = vwap_points[-1] if vwap_points else None
    lower = upper = None
    if last_vwap is not None:
        lower, upper = last_vwap.band(2)

    return SessionLevelsResponse(
        symbol=symbol,
        bar_interval=bar_interval,
        session_date=latest_day,
        previous_high=levels.previous_high,
        previous_low=levels.previous_low,
        previous_close=levels.previous_close,
        premarket_high=levels.premarket_high,
        premarket_low=levels.premarket_low,
        opening_range_high=levels.opening_range_high,
        opening_range_low=levels.opening_range_low,
        regular_open=levels.regular_open,
        vwap=_price(last_vwap.vwap) if last_vwap else None,
        vwap_upper_2sigma=_price(upper),
        vwap_lower_2sigma=_price(lower),
        bars_in_session=len(session_bars),
    )
