"""Cboe delayed option chains, and the fallback that uses them (Phase 98, D117).

Why this exists. The Longbridge account behind this platform can LIST
option contracts but cannot QUOTE them: every `option_quote` call fails
with `301604 no quote access`, and the SDK's own entitlement table says
"USOption: You do not have access to the market's Open API data. Please
visit the Quotes Store to purchase." (measured 2026-09-26). So the options
desk showed a ladder with no market on it, then an error.

Cboe publishes a delayed (~15 minute) quote for every listed contract on
its public delayed-quotes feed - bid, ask, last, volume, open interest,
implied volatility and Cboe's own Greeks - in one document per
underlying. That is enough for a chain desk, for strike selection and for
recording chain snapshots, and it is labelled `cboe-delayed` everywhere so
nobody mistakes a 15-minute-old quote for a live one. It is not enough to
fill an order at, and nothing here pretends it is.

**The Cboe document is fetched once per underlying per minute** and served
for every expiry from memory: one request is ~800 KB and covers the whole
chain, so fetching it per expiry would multiply traffic for no new data.

**`FallbackOptionChainProvider`** asks Longbridge first and falls back to
Cboe when Longbridge refuses to QUOTE. After the first entitlement refusal
it stops asking Longbridge for quotes for the life of the process - a
refusal that is about the account's subscription will not change between
two requests, and repeating it costs a round trip per expiry.

**Expiries in the past are dropped** by both. Longbridge's expiry list on
2026-09-26 still began with 2026-09-23; the desk defaulted to it and the
quote call failed on a contract that no longer trades.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from apps.api.app.marketdata.option_chain_provider import (
    OptionChain,
    OptionChainProvider,
    OptionQuote,
)
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.options.pricing import OptionRight

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{root}.json"
CACHE_SECONDS = 60
_NEW_YORK = ZoneInfo("America/New_York")

# OCC symbol as Cboe prints it: ROOT + YYMMDD + C/P + strike x 1000 (8 digits).
_OCC = re.compile(r"^(?P<root>[A-Z.]+?)(?P<ymd>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")


def today_in_new_york() -> date:
    """US options expire on the New York calendar; a UTC date would treat
    an expiring contract as expired from 20:00 ET the day before."""
    return datetime.now(_NEW_YORK).date()


def cboe_root(underlying: str) -> str:
    """`TQQQ.US` -> `TQQQ`. Only US listings exist on Cboe; anything else is
    refused rather than guessed at."""
    if not underlying.upper().endswith(".US"):
        raise DataUnavailableError(
            f"Cboe lists US options only; {underlying!r} is not a US symbol."
        )
    return underlying.upper()[: -len(".US")]


def _dec(raw: Any) -> Decimal | None:
    """A Cboe number, or None. Cboe prints 0 for an absent bid/ask on an
    untraded contract; a ZERO BID is kept (it is a real quote meaning
    nobody will pay), but a missing field stays None."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _size(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def parse_occ(symbol: str) -> tuple[date, OptionRight, Decimal] | None:
    match = _OCC.match(symbol)
    if match is None:
        return None
    ymd = match.group("ymd")
    try:
        expiry = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    except ValueError:
        return None
    right = OptionRight.CALL if match.group("right") == "C" else OptionRight.PUT
    strike = Decimal(match.group("strike")) / Decimal(1000)
    return expiry, right, strike


class CboeDelayedOptionChainProvider:
    name = "cboe-delayed"

    def __init__(self, fetch: Any | None = None) -> None:
        # `fetch(root) -> dict` is injectable so every rule here is tested
        # without the network; the default is a real HTTP GET.
        self._fetch = fetch or _http_fetch
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def _document(self, underlying: str) -> dict[str, Any]:
        root = cboe_root(underlying)
        cached = self._cache.get(root)
        if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        try:
            doc = await asyncio.to_thread(self._fetch, root)
        except Exception as exc:
            raise VendorError(f"Cboe delayed quotes request failed for {root!r}: {exc}") from exc
        data = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("options"), list):
            raise DataUnavailableError(f"Cboe returned no option chain for {root!r}.")
        self._cache[root] = (time.monotonic(), data)
        return data

    async def get_expiries(self, underlying: str) -> list[date]:
        data = await self._document(underlying)
        today = today_in_new_york()
        out = set()
        for row in data["options"]:
            parsed = parse_occ(str(row.get("option", "")))
            if parsed is not None and parsed[0] >= today:
                out.add(parsed[0])
        return sorted(out)

    async def get_chain(self, underlying: str, expiry: date) -> OptionChain:
        data = await self._document(underlying)
        quotes: list[OptionQuote] = []
        for row in data["options"]:
            symbol = str(row.get("option", ""))
            parsed = parse_occ(symbol)
            if parsed is None or parsed[0] != expiry:
                continue
            _, right, strike = parsed
            quotes.append(
                OptionQuote(
                    contract_symbol=symbol,
                    underlying=underlying,
                    expiry=expiry,
                    strike=strike,
                    right=right,
                    last_price=_dec(row.get("last_trade_price")),
                    bid=_dec(row.get("bid")),
                    ask=_dec(row.get("ask")),
                    volume=_size(row.get("volume")),
                    open_interest=_size(row.get("open_interest")),
                    implied_vol=_dec(row.get("iv")),
                    delta=_dec(row.get("delta")),
                    gamma=_dec(row.get("gamma")),
                    theta=_dec(row.get("theta")),
                    vega=_dec(row.get("vega")),
                )
            )
        if not quotes:
            raise DataUnavailableError(
                f"Cboe lists no {underlying} contracts expiring {expiry.isoformat()}."
            )
        quotes.sort(key=lambda q: (q.strike, q.right.value))
        return OptionChain(
            underlying=underlying,
            expiry=expiry,
            as_of=_as_of(data),
            source=self.name,
            quotes=tuple(quotes),
        )


def _as_of(data: dict[str, Any]) -> datetime:
    """Cboe's own timestamp (New York local), so the desk shows how old the
    quote is rather than when this server happened to read it."""
    raw = data.get("last_trade_time")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=_NEW_YORK).astimezone(UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _http_fetch(root: str) -> dict[str, Any]:
    import httpx

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        response = client.get(
            CBOE_URL.format(root=root), headers={"User-Agent": "trading-os/1.0"}
        )
        response.raise_for_status()
        body = response.json()
    if not isinstance(body, dict):
        raise VendorError(f"Cboe returned a non-object body for {root!r}.")
    return body


ENTITLEMENT_MARKERS = ("301604", "no quote access")


class FallbackOptionChainProvider:
    """Longbridge first; Cboe delayed when Longbridge cannot quote."""

    def __init__(
        self, primary: OptionChainProvider | None, fallback: CboeDelayedOptionChainProvider
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_cannot_quote = primary is None
        self.name = fallback.name if primary is None else f"{primary.name}+{fallback.name}"

    async def get_expiries(self, underlying: str) -> list[date]:
        today = today_in_new_york()
        if self._primary is not None:
            try:
                listed = await self._primary.get_expiries(underlying)
                live = [d for d in listed if d >= today]
                if live:
                    return live
            except (VendorError, DataUnavailableError):
                pass
        return await self._fallback.get_expiries(underlying)

    async def get_chain(self, underlying: str, expiry: date) -> OptionChain:
        if expiry < today_in_new_york():
            raise DataUnavailableError(
                f"{underlying} {expiry.isoformat()} has already expired; choose a current expiry."
            )
        if self._primary is not None and not self._primary_cannot_quote:
            try:
                return await self._primary.get_chain(underlying, expiry)
            except VendorError as exc:
                if not any(m in str(exc) for m in ENTITLEMENT_MARKERS):
                    raise
                self._primary_cannot_quote = True
        return await self._fallback.get_chain(underlying, expiry)
