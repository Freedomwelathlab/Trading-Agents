"""A deterministic, self-consistent fake option chain for Phase 102 tests.

Not market data: a test double with round numbers so every expected fill
and P&L below can be checked by hand. spot 50, strikes 40..60 by 1,
time value max(0.5, 2.0 - 0.1 x |K - spot|), a 2-cent quoted spread, call
delta 0.5 - 0.05 x (K - spot) clamped, put delta = call delta - 1,
open interest 500 everywhere.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from apps.api.app.marketdata.option_chain_provider import OptionChain, OptionQuote
from apps.api.app.options.pricing import OptionRight

SPOT = Decimal("50")


def occ(root: str, expiry: date, right: OptionRight, strike: Decimal) -> str:
    return (
        f"{root}{expiry:%y%m%d}{'C' if right is OptionRight.CALL else 'P'}"
        f"{int(strike * 1000):08d}"
    )


def make_chain(
    expiry: date,
    as_of: datetime,
    *,
    underlying: str = "TQQQ.US",
    spot: Decimal = SPOT,
    overrides: dict[tuple[OptionRight, Decimal], dict] | None = None,
    source: str = "cboe-delayed",
) -> OptionChain:
    overrides = overrides or {}
    root = underlying.split(".")[0]
    quotes = []
    for k in range(40, 61):
        strike = Decimal(k)
        tv = max(Decimal("0.5"), Decimal("2.0") - Decimal("0.1") * abs(strike - spot))
        call_delta = min(Decimal("0.98"), max(Decimal("0.02"),
                                               Decimal("0.5") - Decimal("0.05") * (strike - spot)))
        for right in (OptionRight.CALL, OptionRight.PUT):
            zero = Decimal(0)
            intrinsic = (
                max(spot - strike, zero) if right is OptionRight.CALL else max(strike - spot, zero)
            )
            mid = intrinsic + tv
            fields = dict(
                contract_symbol=occ(root, expiry, right, strike),
                underlying=underlying,
                expiry=expiry,
                strike=strike,
                right=right,
                bid=mid - Decimal("0.01"),
                ask=mid + Decimal("0.01"),
                open_interest=500,
                delta=call_delta if right is OptionRight.CALL else call_delta - 1,
            )
            fields.update(overrides.get((right, strike), {}))
            quotes.append(OptionQuote(**fields))  # type: ignore[arg-type]
    return OptionChain(
        underlying=underlying, expiry=expiry, as_of=as_of, source=source, quotes=tuple(quotes)
    )


class FakeProvider:
    """Serves `chain` for any expiry it lists. Mutable so a test can move the
    market between two bot cycles."""

    name = "stub-delayed"

    def __init__(self, chain: OptionChain, *, expiries: list[date] | None = None):
        self.chain = chain
        self.expiries = expiries if expiries is not None else [chain.expiry]

    async def get_expiries(self, underlying: str) -> list[date]:
        return list(self.expiries)

    async def get_chain(self, underlying: str, expiry: date) -> OptionChain:
        return self.chain
