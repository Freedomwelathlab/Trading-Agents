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

D059 adds two more capabilities from the SAME already-credentialed
vendor - `LongbridgeFundamentalsProvider` (via the SDK's
`AsyncFundamentalContext`) and `LongbridgeNewsProvider` (via
`AsyncContentContext`) - each verified by the same introspection
discipline, and each following the identical "our own Protocol at the
vendor boundary so tests need no credentials" structure.
"""

from collections.abc import Awaitable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from apps.api.app.core.config import Settings
from apps.api.app.marketdata.fundamental_metrics import latest_point
from apps.api.app.marketdata.fundamentals_provider import CompanyFundamentals
from apps.api.app.marketdata.models import MarketSnapshot
from apps.api.app.marketdata.news_provider import NewsHeadline
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError

if TYPE_CHECKING:
    # Type-checking only - the real SDK is never imported at module level
    # (only inside the build_* functions below), but the Protocol below
    # needs the real parameter types to structurally match
    # AsyncQuoteContext.candlesticks exactly, not a widened `object`.
    from longport.openapi import AdjustType, Config, Period


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


def _optional_decimal(raw: object) -> Decimal | None:
    """A vendor metric string -> Decimal, or None when the vendor gave
    nothing usable (empty string, dashes, a non-numeric placeholder).
    Never substitutes a default: an unparseable metric is a missing
    metric, not a zero."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except (ArithmeticError, ValueError):
        return None
    return None if value.is_nan() else value


def _as_utc(raw: object) -> datetime | None:
    """The SDK returns timestamps as `datetime` in some responses and as
    epoch seconds in others (the same defensive handling
    LongbridgeMarketDataProvider.get_snapshot already applies to
    SecurityQuote.timestamp). Anything else is treated as absent rather
    than coerced into a guessed date."""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        try:
            return datetime.fromtimestamp(raw, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _metric_current_value(metric: object) -> tuple[datetime, Decimal] | None:
    """The most recent real data point of one ValuationMetricData series.

    Verified against the installed `longport` package (v4.3.7) by direct
    introspection of `longport/openapi.pyi`, not assumed: `ValuationData`
    exposes `.metrics` (`ValuationMetricsData`) whose `pe`/`pb`/`ps`/
    `dvd_yld` are each `ValuationMetricData | None`, and each of those
    carries `.list: list[ValuationPoint]` where a `ValuationPoint` has
    `.timestamp: datetime` and `.value: str`.

    The point is selected by timestamp via the deterministic
    fundamental_metrics.latest_point(), never by list position.
    """
    if metric is None:
        return None
    raw_points: Any = getattr(metric, "list", None)
    if not raw_points:
        return None

    points: list[tuple[datetime, Decimal]] = []
    for raw_point in raw_points:
        timestamp = _as_utc(getattr(raw_point, "timestamp", None))
        value = _optional_decimal(getattr(raw_point, "value", None))
        if timestamp is not None and value is not None:
            points.append((timestamp, value))

    return latest_point(points)


class LongbridgeFundamentalsClient(Protocol):
    # Not `async def`, same reasoning as LongbridgeQuoteClient.quote -
    # the real SDK's methods are plain methods returning Awaitables.
    def company(self, symbol: str) -> Awaitable[object]: ...
    def valuation(self, symbol: str) -> Awaitable[object]: ...


class LongbridgeFundamentalsProvider:
    """D059: real company fundamentals from the vendor relationship this
    project already has (D008/D015/D021) - no new external service and no
    new credentials.

    Verified against the installed `longport` package (v4.3.7) by direct
    introspection: `AsyncFundamentalContext.create(config)` (synchronous,
    like AsyncQuoteContext.create), `ctx.company(symbol)` (awaitable,
    returns a `CompanyOverview` with `.company_name`/`.name`/`.category`)
    and `ctx.valuation(symbol)` (awaitable, returns a `ValuationData`).

    The two calls are treated independently on purpose: a symbol with a
    company overview but no valuation series still yields a real, partial
    record, and each absent field stays None all the way to the prompt.
    """

    name = "longbridge"

    def __init__(self, client: LongbridgeFundamentalsClient) -> None:
        self._client = client

    async def get_fundamentals(self, symbol: str) -> CompanyFundamentals:
        try:
            overview: Any = await self._client.company(symbol)
        except Exception as exc:
            raise VendorError(
                f"Longbridge company request failed for {symbol!r}: {exc}"
            ) from exc

        try:
            valuation: Any = await self._client.valuation(symbol)
        except Exception as exc:
            raise VendorError(
                f"Longbridge valuation request failed for {symbol!r}: {exc}"
            ) from exc

        company_name = None
        category = None
        if overview is not None:
            company_name = _non_empty_str(
                getattr(overview, "company_name", None)
            ) or _non_empty_str(getattr(overview, "name", None))
            category = _non_empty_str(getattr(overview, "category", None))

        metrics = getattr(valuation, "metrics", None) if valuation is not None else None
        pe_point = _metric_current_value(getattr(metrics, "pe", None))
        pb_point = _metric_current_value(getattr(metrics, "pb", None))
        ps_point = _metric_current_value(getattr(metrics, "ps", None))
        dvd_point = _metric_current_value(getattr(metrics, "dvd_yld", None))

        # as_of is the newest timestamp across the metrics we actually
        # took a value from - not "now", and not a timestamp from a
        # metric whose value we discarded as unparseable.
        timestamps = [
            point[0] for point in (pe_point, pb_point, ps_point, dvd_point) if point is not None
        ]

        fundamentals = CompanyFundamentals(
            symbol=symbol,
            source=self.name,
            company_name=company_name,
            category=category,
            pe=pe_point[1] if pe_point else None,
            pb=pb_point[1] if pb_point else None,
            ps=ps_point[1] if ps_point else None,
            dividend_yield=dvd_point[1] if dvd_point else None,
            as_of=max(timestamps) if timestamps else None,
        )

        if not fundamentals.has_any_data():
            raise DataUnavailableError(
                f"Longbridge returned no usable fundamentals for {symbol!r}."
            )
        return fundamentals


class LongbridgeNewsClient(Protocol):
    # Not `async def`, same reasoning as LongbridgeQuoteClient.quote.
    def news(self, symbol: str) -> Awaitable[Sequence[object]]: ...


class LongbridgeNewsProvider:
    """D059: real recent news headlines for a symbol.

    Verified against the installed `longport` package (v4.3.7) by direct
    introspection: `AsyncContentContext.create(config)` (synchronous) and
    `ctx.news(symbol)` (awaitable, returns `list[NewsItem]`, each with
    `.title`, `.published_at`, `.url`, `.description`).

    Only the title, publication time, and URL are carried forward. The
    vendor's `description` body is deliberately dropped: it would balloon
    the prompt for no analytical gain, and the analyst's job is to
    characterize the flow of headlines it actually saw, not to summarize
    article bodies.
    """

    name = "longbridge"

    def __init__(self, client: LongbridgeNewsClient) -> None:
        self._client = client

    async def get_recent_headlines(self, symbol: str, *, limit: int) -> list[NewsHeadline]:
        if limit < 1:
            raise ValueError("limit must be at least 1")

        try:
            items = await self._client.news(symbol)
        except Exception as exc:
            raise VendorError(f"Longbridge news request failed for {symbol!r}: {exc}") from exc

        headlines: list[NewsHeadline] = []
        for raw in items or []:
            title = _non_empty_str(getattr(raw, "title", None))
            published_at = _as_utc(getattr(raw, "published_at", None))
            # An item with no title or no real publication date is
            # skipped, never back-filled with "now" or "(untitled)" - the
            # analyst cites real dates and counts, so a synthesized one
            # would corrupt exactly the thing that makes the read honest.
            if title is None or published_at is None:
                continue
            headlines.append(
                NewsHeadline(
                    title=title,
                    published_at=published_at,
                    url=_non_empty_str(getattr(raw, "url", None)),
                )
            )

        if not headlines:
            raise DataUnavailableError(f"Longbridge returned no usable news for {symbol!r}.")

        # Newest first, sorted here rather than trusted from the vendor -
        # same reasoning as LongbridgeHistoryProvider's explicit sort.
        headlines.sort(key=lambda headline: headline.published_at, reverse=True)
        return headlines[:limit]


def _non_empty_str(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _longbridge_config(settings: Settings) -> "Config | None":
    """The all-or-nothing credential gate every Longbridge builder shares
    (D015/D021/D059): all three of app key/secret/access token are
    required together, and a partial configuration is treated the same as
    no configuration rather than guessed at.

    Returns the built `Config` (not a bool) so the three secrets are
    narrowed to `str` exactly once, here, instead of each builder
    re-proving it to the type checker. Never logs or echoes any of the
    three values - see docs/TRADING_SAFETY.md's redaction note.
    """
    app_key = settings.longport_app_key
    app_secret = settings.longport_app_secret
    access_token = settings.longport_access_token
    if not (app_key and app_secret and access_token):
        return None

    from longport.openapi import Config

    return Config.from_apikey(
        app_key=app_key, app_secret=app_secret, access_token=access_token
    )


def build_longbridge_provider(settings: Settings) -> LongbridgeMarketDataProvider | None:
    """Returns None - not a fabricated provider - when credentials aren't
    fully configured (see _longbridge_config)."""
    config = _longbridge_config(settings)
    if config is None:
        return None

    from longport.openapi import AsyncQuoteContext

    return LongbridgeMarketDataProvider(AsyncQuoteContext.create(config))


def build_longbridge_history_provider(settings: Settings) -> LongbridgeHistoryProvider | None:
    """Same all-or-nothing credential gate as build_longbridge_provider()
    (D021). Creates its own AsyncQuoteContext rather than sharing one with
    the quote provider - simpler and lower-risk than restructuring D015's
    already-tested construction path; sharing one context between both is
    a reasonable future optimization, not built this phase."""
    config = _longbridge_config(settings)
    if config is None:
        return None

    from longport.openapi import AsyncQuoteContext

    return LongbridgeHistoryProvider(AsyncQuoteContext.create(config))


def build_longbridge_fundamentals_provider(
    settings: Settings,
) -> LongbridgeFundamentalsProvider | None:
    """Same all-or-nothing credential gate as the other Longbridge
    builders (D059). Creates its own AsyncFundamentalContext - a
    different SDK context class from AsyncQuoteContext entirely, so there
    is nothing to share here even in principle."""
    config = _longbridge_config(settings)
    if config is None:
        return None

    # `AsyncFundamentalContext` genuinely exists in longport 4.3.7 (it was
    # verified by direct runtime introspection of the installed package),
    # but the SDK ships an incomplete `openapi.pyi`: the stub declares the
    # synchronous `FundamentalContext` and omits the async one. The ignore
    # is on the vendor's missing stub, not on an unverified attribute.
    from longport.openapi import AsyncFundamentalContext  # type: ignore[attr-defined]

    return LongbridgeFundamentalsProvider(AsyncFundamentalContext.create(config))


def build_longbridge_news_provider(settings: Settings) -> LongbridgeNewsProvider | None:
    """Same all-or-nothing credential gate (D059). News lives on the
    SDK's AsyncContentContext, again a distinct context class."""
    config = _longbridge_config(settings)
    if config is None:
        return None

    from longport.openapi import AsyncContentContext

    return LongbridgeNewsProvider(AsyncContentContext.create(config))
