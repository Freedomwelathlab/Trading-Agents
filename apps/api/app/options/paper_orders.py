"""Paper option order economics (Phase 102, D122). Pure: no I/O.

What an option order on a PAPER broker costs, what it can lose, and what it
settles for - computed from a real (delayed) chain quote, never from a
model and never from a guess.

**Which structures.** Single long calls and puts; a cash-secured short put;
the four verticals (bull call, bear put, bull put, bear call); and the iron
condor. Every one has a loss that is known and bounded at entry. A short
call on its own (unbounded loss) and any leg set that does not match its
declared shape are refused - "no naked shorts" is enforced here, before a
price is ever looked at.

**Fills.** A leg bought fills at `mid + k x half-spread`, a leg sold at
`mid - k x half-spread` (`OPTIONS_PAPER_FILL_HAIRCUT_K`, default 0.5), from
the vendor's own bid and ask. A leg with no two-sided market - bid or ask
missing, a zero bid, or a crossed quote - is refused as DATA_UNAVAILABLE:
there is no price to move from, and inventing one is the thing this code
must never do. Buys round UP to the cent and sells DOWN, so rounding never
flatters the account.

**Capital.** Every structure reserves exactly its max loss from cash at
entry (`capital_per_contract`): a debit structure the debit, a credit
vertical its width less the credit, an iron condor its wider wing less the
credit, a cash-secured put its strike less the premium. That reserve is the
"cash collateral for short legs": the worst settlement a structure can
reach is always covered by what it set aside.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from apps.api.app.marketdata.option_chain_provider import OptionChain, OptionQuote
from apps.api.app.options.pricing import OptionRight
from apps.api.app.options.structures import CONTRACT_MULTIPLIER

MULTIPLIER = Decimal(CONTRACT_MULTIPLIER)
_CENT = Decimal("0.01")
_ZERO = Decimal(0)


class OptionOrderError(Exception):
    """`CODE: human explanation`. The code prefix is what a client switches
    on; the route maps it to a status."""


class OptionStructureType(str, enum.Enum):  # noqa: UP042 (str mixin matches this codebase)
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    CASH_SECURED_PUT = "cash_secured_put"
    BULL_CALL = "bull_call"
    BEAR_PUT = "bear_put"
    BULL_PUT = "bull_put"
    BEAR_CALL = "bear_call"
    IRON_CONDOR = "iron_condor"


class LegSide(str, enum.Enum):  # noqa: UP042
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> Decimal:
        return Decimal(1) if self is LegSide.BUY else Decimal(-1)

    @property
    def opposite(self) -> LegSide:
        return LegSide.SELL if self is LegSide.BUY else LegSide.BUY


@dataclass(frozen=True)
class LegSpec:
    """One leg of an opening order, as requested: which contract (by right
    and strike on the order's expiry) and which side. One contract of each
    leg per structure - ratio spreads are not offered."""

    right: OptionRight
    strike: Decimal
    side: LegSide


@dataclass(frozen=True)
class PricedLeg:
    contract_symbol: str
    right: OptionRight
    strike: Decimal
    side: LegSide
    bid: Decimal
    ask: Decimal
    mid: Decimal
    fill_price: Decimal
    delta: Decimal | None = None


@dataclass(frozen=True)
class StructureEconomics:
    """Per ONE structure (one contract of each leg)."""

    structure_type: OptionStructureType
    legs: tuple[PricedLeg, ...]
    net_price: Decimal
    """Per share, signed: + a debit paid, - a credit received."""
    max_loss_per_contract: Decimal
    """Positive, x100 applied. Equal to the capital the structure reserves."""
    max_profit_per_contract: Decimal | None
    """None where unbounded (long call)."""

    @property
    def capital_per_contract(self) -> Decimal:
        return self.max_loss_per_contract

    @property
    def is_credit(self) -> bool:
        return self.net_price < 0


# --- shape validation ---------------------------------------------------------


def _by(legs: Sequence[LegSpec], right: OptionRight, side: LegSide) -> list[LegSpec]:
    return [leg for leg in legs if leg.right is right and leg.side is side]


def validate_structure(structure: OptionStructureType, legs: Sequence[LegSpec]) -> None:
    """Refuse any leg set that is not exactly the declared defined-risk
    shape. The declared type is not trusted: a "bull_put" whose protective
    put is missing is a naked short put and is refused as one."""
    if not legs:
        raise OptionOrderError("BAD_STRUCTURE: an option order needs at least one leg.")
    if len({(leg.right, leg.strike) for leg in legs}) != len(legs):
        raise OptionOrderError("BAD_STRUCTURE: the same contract appears twice in one order.")
    for leg in legs:
        if leg.strike <= 0:
            raise OptionOrderError("BAD_STRUCTURE: strikes must be positive.")
    if structure is not OptionStructureType.CASH_SECURED_PUT and _uncovered_short(legs):
        raise OptionOrderError(
            "NAKED_SHORT_REFUSED: a sold option with no bought option of the same right "
            "beyond it has an unbounded (call) or uncollateralised (put) loss; paper option "
            "routing takes defined-risk structures only."
        )

    call, put = OptionRight.CALL, OptionRight.PUT
    buy, sell = LegSide.BUY, LegSide.SELL
    s = structure

    def fail(expected: str) -> OptionOrderError:
        naked = any(leg.side is sell for leg in legs)
        code = "NAKED_SHORT_REFUSED" if naked and _uncovered_short(legs) else "BAD_STRUCTURE"
        return OptionOrderError(f"{code}: a {s.value} is {expected}.")

    if s is OptionStructureType.LONG_CALL:
        if len(legs) != 1 or len(_by(legs, call, buy)) != 1:
            raise fail("exactly one bought call")
    elif s is OptionStructureType.LONG_PUT:
        if len(legs) != 1 or len(_by(legs, put, buy)) != 1:
            raise fail("exactly one bought put")
    elif s is OptionStructureType.CASH_SECURED_PUT:
        if len(legs) != 1 or len(_by(legs, put, sell)) != 1:
            raise fail("exactly one sold put, collateralised by its strike in cash")
    elif s in (
        OptionStructureType.BULL_CALL,
        OptionStructureType.BEAR_CALL,
        OptionStructureType.BULL_PUT,
        OptionStructureType.BEAR_PUT,
    ):
        right = call if s in (OptionStructureType.BULL_CALL, OptionStructureType.BEAR_CALL) else put
        longs, shorts = _by(legs, right, buy), _by(legs, right, sell)
        if len(legs) != 2 or len(longs) != 1 or len(shorts) != 1:
            raise fail(f"one bought and one sold {right.value} on the same expiry")
        k_long, k_short = longs[0].strike, shorts[0].strike
        ok = {
            OptionStructureType.BULL_CALL: k_long < k_short,
            OptionStructureType.BEAR_CALL: k_long > k_short,
            OptionStructureType.BULL_PUT: k_long < k_short,
            OptionStructureType.BEAR_PUT: k_long > k_short,
        }[s]
        if not ok:
            raise OptionOrderError(
                f"BAD_STRUCTURE: the strikes given do not form a {s.value} "
                f"(bought {k_long}, sold {k_short})."
            )
    elif s is OptionStructureType.IRON_CONDOR:
        pl, ps = _by(legs, put, buy), _by(legs, put, sell)
        cs, cl = _by(legs, call, sell), _by(legs, call, buy)
        if len(legs) != 4 or not (len(pl) == len(ps) == len(cs) == len(cl) == 1):
            raise fail("a bought put, a sold put, a sold call and a bought call")
        if not (pl[0].strike < ps[0].strike < cs[0].strike < cl[0].strike):
            raise OptionOrderError(
                "BAD_STRUCTURE: an iron condor needs long put < short put < short call "
                "< long call."
            )
    else:  # pragma: no cover - the enum is exhaustive
        raise OptionOrderError(f"BAD_STRUCTURE: unsupported structure {s!r}.")


def _uncovered_short(legs: Sequence[LegSpec]) -> bool:
    """More sold than bought contracts of one right on this expiry. Any
    bought option of the same right and expiry caps a sold one's loss at
    the strike difference (or removes it, when the bought strike is the
    nearer one), so one-for-one pairing is what "covered" means here. A
    single cash-secured put is handled by its own type."""
    for right in (OptionRight.CALL, OptionRight.PUT):
        sold = sum(1 for leg in legs if leg.right is right and leg.side is LegSide.SELL)
        bought = sum(1 for leg in legs if leg.right is right and leg.side is LegSide.BUY)
        if sold > bought:
            return True
    return False


# --- pricing -----------------------------------------------------------------


def two_sided(quote: OptionQuote) -> tuple[Decimal, Decimal] | None:
    """(bid, ask) when the quote is a real two-sided market, else None."""
    bid, ask = quote.bid, quote.ask
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    return bid, ask


def haircut_fill(side: LegSide, *, bid: Decimal, ask: Decimal, k: Decimal) -> Decimal:
    """The modelled paper fill for one leg, rounded against the account."""
    if not Decimal(0) <= k <= Decimal(1):
        raise ValueError("haircut k must be within [0, 1]")
    mid = (bid + ask) / 2
    half = (ask - bid) / 2
    if side is LegSide.BUY:
        return (mid + k * half).quantize(_CENT, rounding=ROUND_CEILING)
    return (mid - k * half).quantize(_CENT, rounding=ROUND_FLOOR)


def find_quote(chain: OptionChain, right: OptionRight, strike: Decimal) -> OptionQuote:
    for q in chain.quotes:
        if q.right is right and q.strike == strike:
            return q
    raise OptionOrderError(
        f"DATA_UNAVAILABLE: {chain.underlying} {chain.expiry.isoformat()} lists no "
        f"{right.value} at strike {strike} ({chain.source})."
    )


def price_leg(quote: OptionQuote, side: LegSide, *, k: Decimal) -> PricedLeg:
    market = two_sided(quote)
    if market is None:
        raise OptionOrderError(
            f"DATA_UNAVAILABLE: {quote.contract_symbol} has no two-sided market "
            f"(bid={quote.bid}, ask={quote.ask}); no fill price is guessed."
        )
    bid, ask = market
    return PricedLeg(
        contract_symbol=quote.contract_symbol,
        right=quote.right,
        strike=quote.strike,
        side=side,
        bid=bid,
        ask=ask,
        mid=(bid + ask) / 2,
        fill_price=haircut_fill(side, bid=bid, ask=ask, k=k),
        delta=quote.delta,
    )


def economics(
    structure: OptionStructureType, legs: Sequence[PricedLeg]
) -> StructureEconomics:
    """Net price, max loss and max profit for ONE structure at these fills.

    Refuses a structure whose fills leave it with no possible profit, or
    that the fills turn into an arbitrage (a credit wider than the spread):
    both mean the quote cannot be traded as the shape it claims to be.
    """
    net = sum((leg.side.sign * leg.fill_price for leg in legs), _ZERO)
    s = structure

    def strike_of(right: OptionRight, side: LegSide) -> Decimal:
        return next(leg.strike for leg in legs if leg.right is right and leg.side is side)

    max_profit: Decimal | None
    if s in (OptionStructureType.LONG_CALL, OptionStructureType.LONG_PUT):
        if net <= 0:
            raise OptionOrderError("BAD_PRICE: a bought option must cost something.")
        max_loss = net * MULTIPLIER
        max_profit = (
            None
            if s is OptionStructureType.LONG_CALL
            else (strike_of(OptionRight.PUT, LegSide.BUY) - net) * MULTIPLIER
        )
    elif s is OptionStructureType.CASH_SECURED_PUT:
        credit = -net
        strike = strike_of(OptionRight.PUT, LegSide.SELL)
        if credit <= 0 or credit >= strike:
            raise OptionOrderError("BAD_PRICE: a sold put must collect a premium below its strike.")
        max_loss = (strike - credit) * MULTIPLIER
        max_profit = credit * MULTIPLIER
    elif s in (OptionStructureType.BULL_CALL, OptionStructureType.BEAR_PUT):
        width = abs(legs[0].strike - legs[1].strike)
        if not _ZERO < net < width:
            raise OptionOrderError(
                f"BAD_PRICE: a {s.value} debit of {net} must be positive and below its "
                f"width {width}, or it has no profit to make."
            )
        max_loss = net * MULTIPLIER
        max_profit = (width - net) * MULTIPLIER
    elif s in (OptionStructureType.BULL_PUT, OptionStructureType.BEAR_CALL):
        width = abs(legs[0].strike - legs[1].strike)
        credit = -net
        if not _ZERO < credit < width:
            raise OptionOrderError(
                f"BAD_PRICE: a {s.value} credit of {credit} must be positive and below its "
                f"width {width}."
            )
        max_loss = (width - credit) * MULTIPLIER
        max_profit = credit * MULTIPLIER
    elif s is OptionStructureType.IRON_CONDOR:
        put_w = strike_of(OptionRight.PUT, LegSide.SELL) - strike_of(OptionRight.PUT, LegSide.BUY)
        call_w = strike_of(OptionRight.CALL, LegSide.BUY) - strike_of(
            OptionRight.CALL, LegSide.SELL
        )
        width = max(put_w, call_w)
        credit = -net
        if not _ZERO < credit < width:
            raise OptionOrderError(
                f"BAD_PRICE: an iron condor credit of {credit} must be positive and below its "
                f"wider wing {width}."
            )
        max_loss = (width - credit) * MULTIPLIER
        max_profit = credit * MULTIPLIER
    else:  # pragma: no cover
        raise OptionOrderError(f"BAD_STRUCTURE: unsupported structure {s!r}.")

    return StructureEconomics(
        structure_type=s,
        legs=tuple(legs),
        net_price=net,
        max_loss_per_contract=max_loss,
        max_profit_per_contract=max_profit,
    )


def price_open(
    structure: OptionStructureType,
    legs: Sequence[LegSpec],
    chain: OptionChain,
    *,
    k: Decimal,
) -> StructureEconomics:
    """Validate, look up every leg on the chain, price it, and compute the
    structure's economics. Raises OptionOrderError with a coded reason."""
    validate_structure(structure, legs)
    priced = [price_leg(find_quote(chain, leg.right, leg.strike), leg.side, k=k) for leg in legs]
    return economics(structure, priced)


def price_close(
    entry_legs: Sequence[dict], chain: OptionChain, *, k: Decimal
) -> tuple[list[PricedLeg], Decimal]:
    """Price closing every leg of an open structure (each traded on the
    opposite side of its entry). Returns the priced legs and the exit value
    per share, signed like entry: exit_value = sum(entry sign x exit fill),
    so realised P&L per share is exit_value - entry_net_price."""
    priced: list[PricedLeg] = []
    exit_value = _ZERO
    for raw in entry_legs:
        right = OptionRight(raw["right"])
        strike = Decimal(str(raw["strike"]))
        entry_side = LegSide(raw["side"])
        quote = find_quote(chain, right, strike)
        leg = price_leg(quote, entry_side.opposite, k=k)
        priced.append(leg)
        exit_value += entry_side.sign * leg.fill_price
    return priced, exit_value


def mid_value(entry_legs: Sequence[dict], chain: OptionChain) -> Decimal | None:
    """The structure's value per share at the quote mids, signed like
    entry; None if any leg has no two-sided market (never a partial mark)."""
    total = _ZERO
    for raw in entry_legs:
        right = OptionRight(raw["right"])
        strike = Decimal(str(raw["strike"]))
        try:
            quote = find_quote(chain, right, strike)
        except OptionOrderError:
            return None
        market = two_sided(quote)
        if market is None:
            return None
        total += LegSide(raw["side"]).sign * (market[0] + market[1]) / 2
    return total


def intrinsic(right: OptionRight, strike: Decimal, underlying: Decimal) -> Decimal:
    if right is OptionRight.CALL:
        return max(underlying - strike, _ZERO)
    return max(strike - underlying, _ZERO)


def settlement_value(entry_legs: Sequence[dict], underlying_close: Decimal) -> Decimal:
    """Per share, signed like entry: what the structure is worth at expiry
    with the underlying settling at `underlying_close`. Exact - expiry has
    no time value - so it is not a model."""
    return sum(
        (
            LegSide(raw["side"]).sign
            * intrinsic(OptionRight(raw["right"]), Decimal(str(raw["strike"])), underlying_close)
            for raw in entry_legs
        ),
        _ZERO,
    )


def realized_pnl(
    *, entry_net_price: Decimal, exit_value: Decimal, quantity: int
) -> Decimal:
    return ((exit_value - entry_net_price) * MULTIPLIER * quantity).quantize(_CENT)


def structure_key(
    underlying: str, structure: OptionStructureType | str, expiry: date, legs: Sequence[LegSpec]
) -> str:
    """The identity the duplicate-order check compares on."""
    kind = structure.value if isinstance(structure, OptionStructureType) else structure
    parts = ",".join(
        f"{leg.side.value[0]}{leg.right.value[0].upper()}{leg.strike.normalize()}"
        for leg in sorted(legs, key=lambda x: (x.right.value, x.strike))
    )
    return f"{underlying}|{kind}|{expiry.isoformat()}|{parts}"[:160]
