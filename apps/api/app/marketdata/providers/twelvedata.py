"""Twelve Data as the FX bar vendor (Phase 106, D132).

IG was the only FX bar source (D123) and its demo API key is suspended, so
FX charts had nothing to draw. Twelve Data's free Basic plan serves forex
(and spot metals such as XAU/USD) with a personal API key: 8 requests per
minute, 800 per day, up to 5,000 bars per request. That is enough for a
chart backfill - a 10-day 5-minute window is ~2,900 bars, one request - but
not for tick-by-tick polling, so this provider is used for BARS only and
the chart refreshes it at most every few minutes.

`EURUSD.FX` is requested as `EUR/USD`. Bars are mid prices with no volume,
exactly like IG's (D123), stamped at the bar's open in UTC (`timezone=UTC`
is sent, so the vendor's exchange-local clock never leaks in). A window
larger than one request is paged forward, pausing to stay inside the
per-minute limit. Nothing is ever padded: a gap stays a gap.

Terms: the free plan is for personal, non-commercial use.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from apps.api.app.core.logging import get_logger
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.fx import fx_pair
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError

logger = get_logger(__name__)

_BASE_URL = "https://api.twelvedata.com"
_TIMEOUT_SECONDS = 20.0
_MAX_OUTPUT = 5000
_PAGE_PAUSE_SECONDS = 8.0  # 8 requests/minute on the free plan

_INTERVALS: dict[str, tuple[str, timedelta]] = {
    "1m": ("1min", timedelta(minutes=1)),
    "5m": ("5min", timedelta(minutes=5)),
    "15m": ("15min", timedelta(minutes=15)),
    "30m": ("30min", timedelta(minutes=30)),
    "1h": ("1h", timedelta(hours=1)),
    "1d": ("1day", timedelta(days=1)),
}


def twelvedata_symbol(symbol: str) -> str:
    """`EURUSD.FX` -> `EUR/USD`; `XAUUSD.FX` -> `XAU/USD`."""
    pair = fx_pair(symbol)
    return f"{pair[:3]}/{pair[3:]}"


def _decimal(value: Any) -> Decimal | None:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d > 0 else None


def _parse_ts(raw: str) -> datetime:
    fmt = "%Y-%m-%d %H:%M:%S" if " " in raw else "%Y-%m-%d"
    return datetime.strptime(raw, fmt).replace(tzinfo=UTC)


class TwelveDataFxBarProvider:
    """`HistoricalBarProvider` for `*.FX` symbols."""

    name = "twelvedata"

    def __init__(self, api_key: str, *, base_url: str = _BASE_URL) -> None:
        if not api_key:
            raise ValueError("TwelveDataFxBarProvider needs an API key.")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def get_bars(
        self, symbol: str, *, bar_interval: str, start_date: date, end_date: date
    ) -> list[Bar]:
        if bar_interval not in _INTERVALS:
            raise ValueError(
                f"Twelve Data FX cannot serve bar_interval {bar_interval!r}; "
                f"supported: {', '.join(_INTERVALS)}."
            )
        vendor_interval, step = _INTERVALS[bar_interval]
        vendor_symbol = twelvedata_symbol(symbol)
        cursor = datetime.combine(start_date, datetime.min.time(), tzinfo=UTC)
        window_end = datetime.combine(end_date, datetime.max.time(), tzinfo=UTC)
        by_ts: dict[datetime, Bar] = {}
        pages = 0
        while cursor <= window_end:
            rows = await self._fetch(vendor_symbol, vendor_interval, cursor, window_end)
            pages += 1
            for row in rows:
                bar = self._row_to_bar(symbol, bar_interval, row)
                if bar is not None and cursor <= bar.ts <= window_end:
                    by_ts[bar.ts] = bar
            if len(rows) < _MAX_OUTPUT or not by_ts:
                break
            cursor = max(by_ts) + step
            await asyncio.sleep(_PAGE_PAUSE_SECONDS)

        if not by_ts:
            raise DataUnavailableError(
                f"Twelve Data returned no {bar_interval} bars for {vendor_symbol} between "
                f"{start_date.isoformat()} and {end_date.isoformat()}."
            )
        bars = sorted(by_ts.values(), key=lambda b: b.ts)
        logger.info(
            "twelvedata_fx_bars_fetched",
            symbol=symbol,
            bar_interval=bar_interval,
            pages=pages,
            bars=len(bars),
        )
        return bars

    async def _fetch(
        self, vendor_symbol: str, interval: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {
                "symbol": vendor_symbol,
                "interval": interval,
                "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end_date": end.strftime("%Y-%m-%d %H:%M:%S"),
                "timezone": "UTC",
                "order": "ASC",
                "outputsize": _MAX_OUTPUT,
                "apikey": self._api_key,
            }
        )
        url = f"{self._base_url}/time_series?{query}"

        def _get() -> Any:
            request = urllib.request.Request(url, headers={"User-Agent": "trading-os"})
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                return json.load(response)

        try:
            body = await asyncio.to_thread(_get)
        except urllib.error.HTTPError as exc:
            # Never echo the URL: it carries the API key.
            raise VendorError(
                f"Twelve Data request for {vendor_symbol} failed: HTTP {exc.code}"
            ) from None
        except Exception as exc:
            raise VendorError(
                f"Twelve Data request for {vendor_symbol} failed: {type(exc).__name__}"
            ) from None

        if not isinstance(body, dict):
            raise VendorError(f"Twelve Data returned a non-object for {vendor_symbol}.")
        if body.get("status") == "error":
            code, message = body.get("code"), str(body.get("message", ""))[:200]
            if code == 400 and "no data" in message.lower():
                return []
            if code == 429:
                raise VendorError(
                    f"Twelve Data rate limit reached (free plan: 8/min, 800/day): {message}"
                )
            raise VendorError(f"Twelve Data refused {vendor_symbol} (code {code}): {message}")
        values = body.get("values")
        return [v for v in values if isinstance(v, dict)] if isinstance(values, list) else []

    @staticmethod
    def _row_to_bar(symbol: str, bar_interval: str, row: dict[str, Any]) -> Bar | None:
        close = _decimal(row.get("close"))
        raw_ts = row.get("datetime")
        if close is None or not isinstance(raw_ts, str):
            return None
        try:
            ts = _parse_ts(raw_ts)
        except ValueError:
            return None
        return Bar(
            symbol=symbol,
            bar_interval=bar_interval,
            ts=ts,
            open=_decimal(row.get("open")),
            high=_decimal(row.get("high")),
            low=_decimal(row.get("low")),
            close=close,
            volume=None,  # spot FX has no consolidated volume (D123)
            source="twelvedata",
        )


def build_twelvedata_fx_bar_provider(settings: Any) -> TwelveDataFxBarProvider | None:
    """The FX leg of `BarBackfillRouter` when `FX_BAR_PROVIDER` is `twelvedata`,
    or `auto` (the default) with `TWELVEDATA_API_KEY` set. None otherwise."""
    choice = getattr(settings, "fx_bar_provider", "auto")
    key = getattr(settings, "twelvedata_api_key", None)
    if choice not in ("twelvedata", "auto"):
        return None
    if not key:
        if choice == "twelvedata":
            logger.warning(
                "fx_bar_provider_not_configured",
                reason="FX_BAR_PROVIDER=twelvedata but TWELVEDATA_API_KEY is not set",
            )
        return None
    return TwelveDataFxBarProvider(key)
