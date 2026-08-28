"""Historical price series port - closes D019's "future work" gap: real
technical indicators need a price *series*, not the single live quote
MarketDataProvider supplies. Deliberately separate from MarketDataProvider
(a different capability, a different Protocol) rather than overloading
get_snapshot() to sometimes mean "one price" and sometimes "many."

Same no-fabrication posture as marketdata/provider.py: implementing this
Protocol is the only sanctioned way to add a source of historical prices,
and its absence must render as DataUnavailableError/VendorError -
reused from marketdata/provider.py rather than a parallel set of error
types, since the failure semantics are identical (this provider was
reachable but had no data vs. this provider itself failed).
"""

from decimal import Decimal
from typing import Protocol


class HistoryProvider(Protocol):
    name: str

    async def get_daily_closes(self, symbol: str, *, count: int) -> list[Decimal]:
        """Returns up to `count` daily closes, oldest first. May return
        fewer than `count` if less history exists - callers must handle a
        short series explicitly (e.g. an indicator needing more points
        than are available), never pad or guess missing values."""
        ...
