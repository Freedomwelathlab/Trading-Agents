"""Yahoo Finance daily history (Phase 108, docs/DECISIONS.md D135).

The Strategy Library measures published daily-bar strategies over decades
and across three home markets - US, Singapore and India - plus crypto and
FX. No vendor already wired here covers that: Longbridge caps a request at
1,000 bars and this account has no SG or India entitlement, Coinbase is
crypto only, Twelve Data's free plan excludes NSE and SGX.

Yahoo's public chart endpoint does cover all of it, with a
dividend-and-split ADJUSTED close, which is what a total-return backtest
needs. It is used here with its limits stated rather than hidden:

  * **Unofficial.** There is no contract, no SLA and no key. Yahoo may
    throttle or refuse a datacentre address at any time; every failure
    surfaces as `VendorError` / `DataUnavailableError`, never as an empty
    success.
  * **Research and end-of-day only.** Daily bars, delayed. Nothing here
    feeds an intraday bot, a quote or an order price.
  * **Never mixed silently.** Every bar and every history carries
    `source="yahoo"`.

Symbols are translated from this platform's grammar, never guessed:

    SPY.US       -> SPY          (US: suffix dropped, class dot -> hyphen)
    D05.SG       -> D05.SI       (Singapore Exchange)
    RELIANCE.IN  -> RELIANCE.NS  (NSE India; `.BSE` -> `.BO`)
    700.HK       -> 0700.HK
    BTC-USD      -> BTC-USD
    EURUSD.FX    -> EURUSD=X
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from apps.api.app.core.logging import get_logger
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.fx import fx_pair, is_fx_symbol
from apps.api.app.marketdata.models import MarketSnapshot
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError

logger = get_logger(__name__)

SOURCE = "yahoo"
_BASE_URL = "https://query1.finance.yahoo.com"
_TIMEOUT_SECONDS = 25.0
_USER_AGENT = "Mozilla/5.0 (compatible; trading-os-research)"

_SUFFIXES: dict[str, str] = {
    "SG": ".SI",
    "IN": ".NS",
    "NSE": ".NS",
    "BSE": ".BO",
    "HK": ".HK",
    "AU": ".AX",
    "UK": ".L",
}


class UnsupportedSymbolError(ValueError):
    """The symbol matches no convention this provider can translate."""


def yahoo_symbol(symbol: str) -> str:
    """This platform's symbol in Yahoo's spelling, or a refusal."""
    raw = symbol.strip().upper()
    if not raw:
        raise UnsupportedSymbolError("empty symbol")
    if raw.startswith("^"):
        return raw
    if is_fx_symbol(raw):
        return f"{fx_pair(raw)}=X"
    if "." not in raw:
        if "-" in raw or "/" in raw:
            return raw.replace("/", "-")
        raise UnsupportedSymbolError(
            f"{symbol!r} has no market suffix. Use e.g. 'SPY.US', 'D05.SG', "
            "'RELIANCE.IN', 'BTC-USD' or 'EURUSD.FX'."
        )
    ticker, _, market = raw.rpartition(".")
    if market == "US":
        return ticker.replace(".", "-")
    if market == "HK":
        return f"{ticker.zfill(4)}.HK"
    suffix = _SUFFIXES.get(market)
    if suffix is None:
        raise UnsupportedSymbolError(
            f"{symbol!r}: market {market!r} is not mapped. Supported: US, "
            + ", ".join(sorted(_SUFFIXES))
            + "."
        )
    return f"{ticker}{suffix}"


@dataclass(frozen=True)
class DailyHistory:
    """One symbol's daily closes, oldest first.

    `adj_closes` is the vendor's dividend-and-split adjusted close: the
    series a total-return backtest must use. `closes` is the traded close,
    which is what an order would have paid. The two differ by design and
    are never substituted for each other.
    """

    symbol: str
    vendor_symbol: str
    currency: str | None
    dates: tuple[date, ...]
    closes: tuple[float, ...]
    adj_closes: tuple[float, ...]
    source: str = SOURCE
    removed: tuple[date, ...] = ()
    """Sessions a quality screen dropped as vendor bad ticks. Empty as
    fetched; filled by `library.quality.screen_history`, never here."""
    truncated_before: date | None = None
    """Set by the same screen when everything before this date was
    discarded because it held a jump that could not be explained."""

    def __len__(self) -> int:
        return len(self.dates)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


class YahooDailyProvider:
    """Daily bars and adjusted daily history. `1d` only, by construction."""

    name = SOURCE

    def __init__(self, *, base_url: str = _BASE_URL) -> None:
        self._base_url = base_url.rstrip("/")

    async def _chart(self, vendor_symbol: str, start: date, end: date) -> dict[str, Any]:
        period1 = int(datetime.combine(start, datetime.min.time(), tzinfo=UTC).timestamp())
        period2 = int(
            datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC).timestamp()
        )
        query = urllib.parse.urlencode(
            {"period1": period1, "period2": period2, "interval": "1d", "events": "div,split"}
        )
        path = urllib.parse.quote(vendor_symbol, safe="^=.-")
        url = f"{self._base_url}/v8/finance/chart/{path}?{query}"

        def _get() -> Any:
            request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                return json.load(response)

        try:
            body = await asyncio.to_thread(_get)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise DataUnavailableError(f"Yahoo has no symbol {vendor_symbol!r}.") from None
            if exc.code == 429:
                raise VendorError(
                    "Yahoo is rate-limiting this address (HTTP 429). It is an "
                    "unofficial feed with no quota guarantee - retry later."
                ) from None
            raise VendorError(
                f"Yahoo request for {vendor_symbol} failed: HTTP {exc.code}"
            ) from None
        except Exception as exc:
            raise VendorError(
                f"Yahoo request for {vendor_symbol} failed: {type(exc).__name__}"
            ) from None

        chart = body.get("chart") if isinstance(body, dict) else None
        if not isinstance(chart, dict):
            raise VendorError(f"Yahoo returned an unexpected body for {vendor_symbol}.")
        if chart.get("error"):
            description = str((chart["error"] or {}).get("description", ""))[:200]
            raise DataUnavailableError(f"Yahoo refused {vendor_symbol}: {description}")
        results = chart.get("result")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            raise DataUnavailableError(f"Yahoo returned no result for {vendor_symbol}.")
        return results[0]

    async def get_daily_history(self, symbol: str, *, start: date, end: date) -> DailyHistory:
        """Adjusted daily history in [start, end]. Rows with no close (a
        vendor gap) are dropped, never filled."""
        vendor_symbol = yahoo_symbol(symbol)
        result = await self._chart(vendor_symbol, start, end)
        stamps = result.get("timestamp") or []
        indicators = result.get("indicators") or {}
        quote = (indicators.get("quote") or [{}])[0] or {}
        closes = quote.get("close") or []
        adj = ((indicators.get("adjclose") or [{}])[0] or {}).get("adjclose") or []
        meta = result.get("meta") or {}
        offset = int(meta.get("gmtoffset") or 0)

        by_date: dict[date, tuple[float, float]] = {}
        for i, stamp in enumerate(stamps):
            close = closes[i] if i < len(closes) else None
            if not isinstance(close, (int, float)) or close <= 0:
                continue
            adjusted = adj[i] if i < len(adj) else None
            if not isinstance(adjusted, (int, float)) or adjusted <= 0:
                adjusted = close
            # The exchange's own calendar day, not UTC's: an SGX or NSE
            # session opens on the previous UTC evening.
            day = datetime.fromtimestamp(int(stamp) + offset, UTC).date()
            if start <= day <= end:
                by_date[day] = (float(close), float(adjusted))
        if not by_date:
            raise DataUnavailableError(
                f"Yahoo returned no daily closes for {vendor_symbol} between "
                f"{start.isoformat()} and {end.isoformat()}."
            )
        days = sorted(by_date)
        logger.info("yahoo_daily_history_fetched", symbol=symbol, bars=len(days))
        return DailyHistory(
            symbol=symbol,
            vendor_symbol=vendor_symbol,
            currency=meta.get("currency"),
            dates=tuple(days),
            closes=tuple(by_date[d][0] for d in days),
            adj_closes=tuple(by_date[d][1] for d in days),
        )

    async def get_quote(self, symbol: str) -> MarketSnapshot:
        """The vendor's latest price with the vendor's own timestamp.

        DELAYED for most exchanges (often 15 minutes) and labelled so. It is
        offered for paper fills of daily strategies and for valuation; the
        caller decides how old is too old - the timestamp is never
        refreshed or rounded up to "now".
        """
        vendor_symbol = yahoo_symbol(symbol)
        today = datetime.now(UTC).date()
        result = await self._chart(vendor_symbol, today - timedelta(days=7), today)
        meta = result.get("meta") or {}
        price = _decimal(meta.get("regularMarketPrice"))
        stamp = meta.get("regularMarketTime")
        if price is None or price <= 0 or not isinstance(stamp, (int, float)):
            raise DataUnavailableError(f"Yahoo returned no current price for {vendor_symbol}.")
        return MarketSnapshot(
            symbol=symbol,
            price=price,
            as_of=datetime.fromtimestamp(int(stamp), UTC),
            source=f"{SOURCE}-delayed",
        )

    async def get_bars(
        self, symbol: str, *, bar_interval: str, start_date: date, end_date: date
    ) -> list[Bar]:
        """`HistoricalBarProvider`: real traded OHLCV, unadjusted."""
        if bar_interval != "1d":
            raise ValueError(f"Yahoo is wired for daily bars only, not {bar_interval!r}.")
        vendor_symbol = yahoo_symbol(symbol)
        result = await self._chart(vendor_symbol, start_date, end_date)
        stamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
        bars: list[Bar] = []
        for i, stamp in enumerate(stamps):
            close = _decimal((quote.get("close") or [None] * (i + 1))[i])
            if close is None or close <= 0:
                continue
            volume = (quote.get("volume") or [None] * (i + 1))[i]
            bars.append(
                Bar(
                    symbol=symbol,
                    bar_interval="1d",
                    ts=datetime.fromtimestamp(int(stamp), UTC),
                    open=_decimal((quote.get("open") or [None] * (i + 1))[i]),
                    high=_decimal((quote.get("high") or [None] * (i + 1))[i]),
                    low=_decimal((quote.get("low") or [None] * (i + 1))[i]),
                    close=close,
                    volume=int(volume) if isinstance(volume, (int, float)) else None,
                    source=SOURCE,
                )
            )
        if not bars:
            raise DataUnavailableError(
                f"Yahoo returned no daily bars for {vendor_symbol} between "
                f"{start_date.isoformat()} and {end_date.isoformat()}."
            )
        return bars
