"""Order-book depth: the read contract, and the empty-book trap it exists
to make visible (Phase 74, D092).

Depth is the one market-data capability on this platform where the vendor
answers **successfully with nothing**. `QuoteContext.depth("TQQQ.US")`
returns HTTP 200 and a structurally valid object containing one bid level
and one ask level - whose `price` is `None` and whose `volume` is `0`.

Measured, on this account, at 01:33 ET with the US market closed:

    TQQQ.US   1 bid level, 1 ask level, top bid None x 0
    AAPL.US   1 bid level, 1 ask level, top bid None x 0
    700.HK    1 bid level, 1 ask level, top bid 434.200 x 5700

**Two explanations fit that observation and this module deliberately does
not choose between them.** The account holds `LV1 Real-time Quotes` for US
and LV1/real-time for HK, and an LV1 entitlement generally carries no depth
ladder at all; but the US market was also closed at the time of
measurement, while Hong Kong was open. One observation cannot separate
"this entitlement has no book" from "this market has no book right now",
and asserting either would be a guess presented as a fact.

It does not need to choose, because the correct handling is identical
either way: **a level whose price is absent is not a level.** Rendering it
as `0.00 x 0` would draw a book that looks real, tight and empty - the
most dangerous possible reading, because a zero bid is a price, and a
spread of zero is a number a human or a strategy can act on. So a response
whose levels carry no price is reported as DATA_UNAVAILABLE, exactly like
a quote the vendor could not supply, and the caller sees the same honest
sentinel it already knows how to render.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class DepthLevel:
    """One price level of the book.

    `price` is non-optional here BY CONSTRUCTION: a level that reached this
    type has a price. Filtering happens at the vendor boundary so nothing
    downstream has to keep re-deciding whether a level is real.
    """

    price: Decimal
    volume: int
    order_count: int | None = None


@dataclass(frozen=True)
class OrderBook:
    """Both sides of the book at one instant, best price first."""

    symbol: str
    bids: tuple[DepthLevel, ...]
    asks: tuple[DepthLevel, ...]
    as_of: datetime
    source: str

    @property
    def best_bid(self) -> DepthLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> DepthLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def spread(self) -> Decimal | None:
        """`None` unless BOTH sides are present.

        A one-sided book has no spread. Substituting zero, or measuring
        against the last trade, would produce a number that reads as a
        real spread and is not one.
        """
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return ask.price - bid.price

    @property
    def is_empty(self) -> bool:
        return not self.bids and not self.asks


class DepthProvider(Protocol):
    """Raises `DataUnavailableError` when the vendor has no priced level,
    the same contract `MarketDataProvider.get_snapshot` uses for a quote it
    cannot supply - so the route layer needs no special case."""

    name: str

    async def get_depth(self, symbol: str) -> OrderBook: ...
