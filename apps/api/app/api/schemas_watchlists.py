"""DTOs for the watchlist routes (Phase 50).

The quote payload is the part worth reading carefully. `WatchlistQuote`
has BOTH an optional `price` block and an optional `unavailable` sentinel,
and exactly one of them is ever populated. That is deliberate and is the
whole no-fabrication contract of this feature in one type: there is no
shape this model can take in which a symbol carries a number that did not
come from a real `MarketSnapshot`. A symbol the vendor could not price
comes back as a row with `price: null` and a `DATA_UNAVAILABLE:`-prefixed
string saying why - never a dropped row (which would quietly shorten the
user's own list) and never a stale, zero, or interpolated price.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

MAX_WATCHLIST_NAME_LENGTH = 64
MAX_SYMBOL_LENGTH = 32


def normalize_symbol(raw: str) -> str:
    """Trim and upper-case. Applied on the way in so
    `uq_watchlist_item_watchlist_symbol` is a real constraint rather than
    one that "aapl" walks around, and so the symbol persisted is the same
    string handed to the market-data vendor."""
    return raw.strip().upper()


class CreateWatchlistRequest(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_WATCHLIST_NAME_LENGTH)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class AddWatchlistItemRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=MAX_SYMBOL_LENGTH)

    @field_validator("symbol")
    @classmethod
    def _normalize(cls, v: str) -> str:
        normalized = normalize_symbol(v)
        if not normalized:
            raise ValueError("symbol must not be blank")
        return normalized


class WatchlistResponse(BaseModel):
    """One watchlist and the symbols currently on it.

    `symbols` is included here rather than behind a separate
    `GET /watchlists/{id}` so the frontend can render the whole panel from
    one request. It is the stored list only - no prices, no vendor call.
    Prices live on `GET /watchlists/{id}/quotes` and nowhere else, which
    keeps the cheap read cheap and makes it impossible for a listing to
    imply a price it never fetched.
    """

    id: uuid.UUID
    name: str
    created_at: datetime
    symbols: list[str]


class ListWatchlistsResponse(BaseModel):
    """Same `{items, limit, offset}` envelope as D027's portfolio history,
    D031's admin listings and D034's broker discovery - one pagination
    convention in this codebase, not four."""

    watchlists: list[WatchlistResponse]
    limit: int
    offset: int


class WatchlistQuote(BaseModel):
    """One symbol's live quote, or an explicit statement that there is
    none.

    `price`/`as_of`/`source` are copied straight off a real
    `MarketSnapshot` and are all three null together. `unavailable` is
    null exactly when they are set, and is a `DATA_UNAVAILABLE:`-prefixed
    string carrying the real underlying cause (the router's own
    `NO_DATA_AVAILABLE:` message, or the `NOT_CONFIGURED:` string used
    when no vendor is wired at all) when they are not.
    """

    symbol: str
    price: Decimal | None = None
    as_of: datetime | None = None
    source: str | None = None
    unavailable: str | None = None


class WatchlistQuotesResponse(BaseModel):
    """Every symbol on the watchlist, in the same stored order, each with
    its own outcome.

    The list length always equals the watchlist's length: an unpriceable
    symbol occupies its row and says so. `market_data_configured` reports
    whether any vendor is wired at all (D008/D015) - when it is false
    every row is unavailable for that one reason, and a client can say so
    once instead of repeating the same sentinel N times.
    """

    watchlist_id: uuid.UUID
    name: str
    market_data_configured: bool
    quotes: list[WatchlistQuote]
