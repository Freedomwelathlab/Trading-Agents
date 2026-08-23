"""Market data provider port. No concrete vendor is wired into this
codebase yet - no vendor credentials exist, and none should be invented.
Implementing this Protocol is the only sanctioned way to add one later.
"""

from typing import Protocol

from apps.api.app.marketdata.models import MarketSnapshot


class DataUnavailableError(Exception):
    """The provider was reachable but has no data for this symbol (e.g. an
    unlisted or delisted ticker). Distinct from VendorError so logs/callers
    can tell 'this symbol doesn't exist here' apart from 'the vendor is
    broken right now'."""


class VendorError(Exception):
    """The provider itself failed - network error, auth failure, rate
    limit, malformed response. Never caught and replaced with a guessed
    price; the router treats this as a reason to try the next provider,
    nothing more."""


class MarketDataProvider(Protocol):
    name: str

    async def get_snapshot(self, symbol: str) -> MarketSnapshot: ...
