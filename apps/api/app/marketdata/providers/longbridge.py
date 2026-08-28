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
from typing import TYPE_CHECKING, Any, Protocol

from apps.api.app.core.config import Settings
from apps.api.app.marketdata.models import MarketSnapshot
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError

if TYPE_CHECKING:
    # Type-checking only - the real SDK is never imported at module level
    # (only inside the build_* functions below), but the Protocol below
    # needs the real parameter types to structurally match
    # AsyncQuoteContext.candlesticks exactly, not a widened `object`.
    from longport.openapi import AdjustType, Period


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


class LongbridgeCandlestickClient(Protocol):
    # Not `async def`, same reasoning as LongbridgeQuoteClient.quote above -
    # the real SDK's .candlesticks() isn't itself a coroutine function.
    def candlesticks(
        self, symbol: str, period: "type[Period]", count: int, adjust_type: "type[AdjustType]"
    ) -> Awaitable[Sequence[object]]: ...


class LongbridgeHistoryProvider:
    """D021: real daily closes for computing an actual deterministic
    technical indicator, closing D019's "no historical price data" gap.
    Verified against the installed `longport` package (v4.3.7) by direct
    introspection: `AsyncQuoteContext.candlesticks(symbol, period, count,
    adjust_type)` (awaitable, returns `Candlestick` objects with `.close`/
    `.timestamp`), `Period.Day`, `AdjustType.NoAdjust`."""

    name = "longbridge"

    def __init__(self, client: LongbridgeCandlestickClient) -> None:
        self._client = client

    async def get_daily_closes(self, symbol: str, *, count: int) -> list[Decimal]:
        from longport.openapi import AdjustType, Period

        try:
            candles = await self._client.candlesticks(
                symbol, Period.Day, count, AdjustType.NoAdjust
            )
        except Exception as exc:
            raise VendorError(
                f"Longbridge candlestick request failed for {symbol!r}: {exc}"
            ) from exc

        if not candles:
            raise DataUnavailableError(f"Longbridge returned no candlesticks for {symbol!r}.")

        # Never assume the SDK's ordering - sort by timestamp ourselves so
        # a series is always oldest-first regardless of what the API
        # happens to return, matching HistoryProvider's documented contract.
        rows: list[Any] = list(candles)
        rows.sort(key=lambda c: c.timestamp)
        return [Decimal(str(c.close)) for c in rows]


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


def build_longbridge_history_provider(settings: Settings) -> LongbridgeHistoryProvider | None:
    """Same all-or-nothing credential gate as build_longbridge_provider()
    (D021). Creates its own AsyncQuoteContext rather than sharing one with
    the quote provider - simpler and lower-risk than restructuring D015's
    already-tested construction path; sharing one context between both is
    a reasonable future optimization, not built this phase."""
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
    return LongbridgeHistoryProvider(context)
