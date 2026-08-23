"""Longbridge (LongPort OpenAPI) market data provider - the first concrete
MarketDataProvider, closing D008. See docs/DECISIONS.md D015.

`LongbridgeQuoteClient` is the minimal slice of `longport.openapi`'s
`AsyncQuoteContext` this provider actually depends on, expressed as our
own Protocol so tests can inject a stub without real Longbridge
credentials or a network call - the real SDK is only imported by
`build_longbridge_provider()`, never by the provider class itself or by
tests.

Verified against the installed `longport` package (v4.3.7) by direct
introspection, not assumed from documentation: `Config.from_apikey(...)`,
`AsyncQuoteContext.create(config)` (synchronous - no await), and
`ctx.quote([symbols])` (awaitable, returns one `SecurityQuote` per
symbol with `.last_done` and `.timestamp`).
"""

from collections.abc import Awaitable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from apps.api.app.core.config import Settings
from apps.api.app.marketdata.models import MarketSnapshot
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError


class LongbridgeQuoteClient(Protocol):
    # Not `async def` - the real SDK's `.quote()` isn't a coroutine
    # function itself, it's a plain method returning an Awaitable (mypy
    # treats those as structurally different for Protocol matching).
    def quote(self, symbols: list[str]) -> Awaitable[Sequence[object]]: ...


class LongbridgeMarketDataProvider:
    name = "longbridge"

    def __init__(self, client: LongbridgeQuoteClient) -> None:
        self._client = client

    async def get_snapshot(self, symbol: str) -> MarketSnapshot:
        try:
            results = await self._client.quote([symbol])
        except Exception as exc:
            # The SDK raises its own OpenApiException (network/auth/rate-limit
            # failures) - never let an unrecognized exception type escape
            # untyped, since MarketDataRouter only treats VendorError and
            # DataUnavailableError as "try the next provider."
            raise VendorError(f"Longbridge request failed for {symbol!r}: {exc}") from exc

        if not results:
            raise DataUnavailableError(f"Longbridge returned no quote for {symbol!r}.")

        raw: Any = results[0]
        price = Decimal(str(raw.last_done))
        if price <= 0:
            raise DataUnavailableError(
                f"Longbridge returned a non-positive last_done for {symbol!r}: {price}."
            )

        as_of = raw.timestamp
        if not isinstance(as_of, datetime):
            as_of = datetime.fromtimestamp(as_of, tz=UTC)
        elif as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=UTC)

        return MarketSnapshot(symbol=symbol, price=price, as_of=as_of, source=self.name)


def build_longbridge_provider(settings: Settings) -> LongbridgeMarketDataProvider | None:
    """Returns None - not a fabricated provider - when credentials aren't
    fully configured. All three of app key/secret/access token are
    required together; a partial configuration is treated the same as no
    configuration rather than guessed at."""
    if not (
        settings.longport_app_key
        and settings.longport_app_secret
        and settings.longport_access_token
    ):
        return None

    from longport.openapi import AsyncQuoteContext, Config

    config = Config.from_apikey(
        app_key=settings.longport_app_key,
        app_secret=settings.longport_app_secret,
        access_token=settings.longport_access_token,
    )
    context = AsyncQuoteContext.create(config)
    return LongbridgeMarketDataProvider(context)
