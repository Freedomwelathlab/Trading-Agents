"""Option chain port (Phase 89, D108).

The shape of an option chain, and the Protocol a vendor adapter must
satisfy to serve one. No vendor is imported here, for the same reason
`marketdata/provider.py` imports none: this file defines what the platform
needs, and an adapter elsewhere decides how to get it.

Three rules carry over from the equity side unchanged, and each one exists
because its opposite produces a confident wrong answer:

**A missing field stays None.** An option with no bid has no bid — it is
not an option bid at zero. A chain row rendered with `0.00` where the
vendor returned nothing reads as a contract nobody will pay for, which is
a claim about the market rather than about the feed. Every price, size and
Greek below is therefore nullable.

**Greeks are the VENDOR's when the vendor supplies them.** This codebase
can compute its own (`apps/api/app/options/pricing.py`) and deliberately
does not substitute them here: a delta computed from a different
volatility input than the one the desk is quoting is a different delta,
and mixing the two in one table makes the column meaningless. `source`
records which feed each row came from. Computing Greeks for a row that has
none is a separate, explicit act at the point of use, never a silent fill.

**An empty chain is an empty chain.** A vendor that answers with no
strikes means this expiry has no listed contracts we can see - possibly an
entitlement limit, possibly a symbol with no options at all. Both are
reported as DATA_UNAVAILABLE by the route; neither becomes a synthesised
ladder around the spot price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from apps.api.app.options.pricing import OptionRight


@dataclass(frozen=True)
class OptionQuote:
    """One contract, as the vendor reported it."""

    contract_symbol: str
    underlying: str
    expiry: date
    strike: Decimal
    right: OptionRight
    last_price: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume: int | None = None
    open_interest: int | None = None
    implied_vol: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None

    @property
    def mid(self) -> Decimal | None:
        """The mid, or None when either side is missing.

        A "mid" taken from one side alone is that side's price wearing a
        neutral name, and it is exactly the number a spread's cost would be
        computed from — so it is refused rather than approximated.
        """
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass(frozen=True)
class OptionChain:
    underlying: str
    expiry: date
    as_of: datetime
    source: str
    quotes: tuple[OptionQuote, ...]

    def by_right(self, right: OptionRight) -> tuple[OptionQuote, ...]:
        return tuple(q for q in self.quotes if q.right is right)

    @property
    def strikes(self) -> tuple[Decimal, ...]:
        return tuple(sorted({q.strike for q in self.quotes}))


class OptionChainProvider(Protocol):
    name: str

    async def get_expiries(self, underlying: str) -> list[date]:
        """Listed expiries for this underlying, ascending.

        An empty list is a real answer meaning "this vendor lists no
        options on this symbol", and callers must render it as such rather
        than as an error or as a default expiry.
        """
        ...

    async def get_chain(self, underlying: str, expiry: date) -> OptionChain: ...
