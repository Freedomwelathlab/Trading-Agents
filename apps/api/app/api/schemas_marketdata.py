from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from apps.api.app.marketdata.bar_provider import BarInterval


class QuoteResponse(BaseModel):
    symbol: str
    price: Decimal
    as_of: datetime
    source: str


class BarResponse(BaseModel):
    """One persisted OHLCV bar, exactly as ingested (Phase 70, D088).

    `open`/`high`/`low`/`volume` are nullable because the underlying column
    is - a vendor may return a close-only record - and they are passed
    through as `null` rather than being backfilled from the close. A chart
    that receives a null high must draw nothing there; drawing a candle
    whose body and wicks were all derived from one number would render
    fabricated market structure.

    `source` travels with every bar so a suspicious value on a chart can be
    traced to the vendor that produced it, the same reasoning
    `Bar.source` and `MarketSnapshot.source` already state.
    """

    ts: datetime
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal
    volume: int | None
    source: str


class BarsResponse(BaseModel):
    """The bars this system HAS for a symbol and interval over a window -
    never the bars it was asked for.

    An empty `bars` list with a 200 is a real, expected answer meaning "no
    bars are ingested here", and is deliberately not a 404: the symbol may
    be perfectly valid and simply un-backfilled, and the two are different
    problems with different fixes. `count` is stated explicitly so a client
    does not have to infer "we got fewer than we asked for" from a date
    range and a weekend calendar it does not have.
    """

    symbol: str
    bar_interval: BarInterval
    count: int
    bars: list[BarResponse]
