"""Live crypto quote and order book from Coinbase's public API (Phase 106, D129).

The Markets page asked Longbridge for `BTC-USD` depth and got 301600
"invalid symbol": Longbridge has no spot crypto on this account (D089), and
the quote and depth providers were Longbridge-only. Coinbase Exchange
publishes both without credentials:

* ticker - `GET /products/{id}/ticker` (last trade price and time)
* book   - `GET /products/{id}/book?level=2` (aggregated levels, best first)

Public endpoints are rate-limited per IP (about 10 requests/second); the
Markets page polls every 5 s, far inside that.

**Symbols.** A crypto symbol is `BASE-QUOTE` (Coinbase's own product id) or
`BASE/QUOTE` (the spelling Kraken and most charts use); both map to the
same product id. `USDT` is not rewritten to `USD` - they are different
assets and Coinbase lists them as different products.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from apps.api.app.marketdata.depth_provider import DepthLevel, OrderBook
from apps.api.app.marketdata.models import MarketSnapshot
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError

_BASE_URL = "https://api.exchange.coinbase.com"
_TIMEOUT_SECONDS = 8.0
_BOOK_LEVELS = 20
_CRYPTO = re.compile(r"^[A-Z0-9]{2,10}[-/][A-Z0-9]{2,6}$")


def is_crypto_symbol(symbol: str) -> bool:
    """`BTC-USD` or `BTC/USD`: a hyphen or slash and no market-suffix dot."""
    return bool(_CRYPTO.match(symbol.strip().upper()))


def product_id(symbol: str) -> str:
    """Coinbase's product id for a crypto symbol (`BTC/USD` -> `BTC-USD`)."""
    return symbol.strip().upper().replace("/", "-")


def _decimal(value: Any) -> Decimal | None:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


class _CoinbasePublic:
    def __init__(self, *, base_url: str = _BASE_URL) -> None:
        self._base_url = base_url.rstrip("/")

    def supports(self, symbol: str) -> bool:
        return is_crypto_symbol(symbol)

    async def _get_json(self, path: str, symbol: str) -> Any:
        url = f"{self._base_url}{path}"

        def _get() -> Any:
            request = urllib.request.Request(
                url, headers={"User-Agent": "trading-os", "Accept": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                return json.load(response)

        try:
            return await asyncio.to_thread(_get)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode("utf-8", "replace")
            if exc.code == 404:
                raise DataUnavailableError(
                    f"Coinbase lists no product {product_id(symbol)!r} ({detail})."
                ) from exc
            raise VendorError(
                f"Coinbase request for {symbol!r} failed: HTTP {exc.code} {detail}"
            ) from exc
        except Exception as exc:
            raise VendorError(f"Coinbase request for {symbol!r} failed: {exc}") from exc


class CoinbaseQuoteProvider(_CoinbasePublic):
    """`MarketDataProvider` for crypto symbols: the last traded price."""

    name = "coinbase"

    async def get_snapshot(self, symbol: str) -> MarketSnapshot:
        if not is_crypto_symbol(symbol):
            raise DataUnavailableError(f"{symbol!r} is not a crypto symbol.")
        pid = urllib.parse.quote(product_id(symbol))
        body = await self._get_json(f"/products/{pid}/ticker", symbol)
        price = _decimal(body.get("price")) if isinstance(body, dict) else None
        if price is None or price <= 0:
            raise DataUnavailableError(f"Coinbase returned no price for {symbol!r}.")
        as_of = datetime.now(UTC)
        raw_time = body.get("time")
        if isinstance(raw_time, str):
            try:
                as_of = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            except ValueError:
                pass
        return MarketSnapshot(symbol=symbol, price=price, as_of=as_of, source="coinbase")


class CoinbaseDepthProvider(_CoinbasePublic):
    """`DepthProvider` for crypto symbols: the aggregated level-2 book."""

    name = "coinbase"

    async def get_depth(self, symbol: str) -> OrderBook:
        if not is_crypto_symbol(symbol):
            raise DataUnavailableError(f"{symbol!r} is not a crypto symbol.")
        pid = urllib.parse.quote(product_id(symbol))
        body = await self._get_json(f"/products/{pid}/book?level=2", symbol)
        if not isinstance(body, dict):
            raise VendorError(f"Coinbase returned a non-object book for {symbol!r}.")

        def levels(rows: Any) -> tuple[DepthLevel, ...]:
            out: list[DepthLevel] = []
            for row in rows or []:
                if not isinstance(row, list) or len(row) < 2:
                    continue
                price, size = _decimal(row[0]), _decimal(row[1])
                if price is None or size is None or price <= 0 or size <= 0:
                    continue  # never a zero-priced or empty level (D092)
                count = row[2] if len(row) > 2 and isinstance(row[2], int) else None
                out.append(DepthLevel(price=price, volume=size, order_count=count))
                if len(out) >= _BOOK_LEVELS:
                    break
            return tuple(out)

        bids, asks = levels(body.get("bids")), levels(body.get("asks"))
        if not bids and not asks:
            raise DataUnavailableError(f"Coinbase returned an empty book for {symbol!r}.")
        return OrderBook(
            symbol=symbol, bids=bids, asks=asks, as_of=datetime.now(UTC), source="coinbase"
        )
