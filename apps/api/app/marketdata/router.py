"""Vendor routing with an explicit fallback chain and a hard no-fabrication
floor: if every configured provider fails, this raises rather than
returning any kind of guessed or stale-cached price (spec Sec57).
"""

from collections.abc import Sequence

from apps.api.app.marketdata.models import MarketSnapshot
from apps.api.app.marketdata.provider import (
    DataUnavailableError,
    MarketDataProvider,
    VendorError,
)


class NoDataAvailableError(Exception):
    """Every configured provider failed or had no data. The message is
    prefixed NO_DATA_AVAILABLE: by convention so it's grep-able in logs and
    unmistakable in any surface that renders it - this is the sentinel
    spec Sec57 requires in place of a fabricated value."""


class MarketDataRouter:
    def __init__(self, providers: Sequence[MarketDataProvider]) -> None:
        if not providers:
            raise ValueError(
                "MarketDataRouter requires at least one provider. An empty "
                "router would silently mean 'no market data is ever "
                "available' with no explicit configuration signal - that's "
                "a config bug, not a valid state to construct."
            )
        self._providers = list(providers)

    async def get_snapshot(self, symbol: str) -> MarketSnapshot:
        attempts: list[str] = []
        for provider in self._providers:
            try:
                return await provider.get_snapshot(symbol)
            except (DataUnavailableError, VendorError) as exc:
                attempts.append(f"{provider.name}: {exc}")
                continue

        raise NoDataAvailableError(
            f"NO_DATA_AVAILABLE: no configured provider returned data for "
            f"{symbol!r}. Attempts: {'; '.join(attempts)}"
        )
