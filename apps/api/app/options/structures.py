"""Defined-risk option structures (Phase 75, D093).

The playbook's hard rule is that the automated system trades DEFINED-RISK
structures only — "never sell naked TQQQ options." Every structure here has
a max loss that is known and bounded at entry, and the constructors refuse
to build anything whose loss is not capped.

Leg prices come from the Black-Scholes model in `pricing.py`, so a
structure's `net_premium`, `max_profit`, `max_loss` and `breakeven` are
MODELED figures for backtesting, not a quote. Money is expressed in
`Decimal` here because at this boundary the numbers stand for an actual
premium or capital-at-risk a position would carry; the float model is
converted in exactly one place (`_money`).

The four verticals are the debit/credit pairs the reversal and premium
paths use. `iron_condor` is included for the premium path's neutral case
but is flagged non-native: the playbook notes Longbridge's multi-leg API
supports verticals/straddle/strangle/collar but NOT iron condor/calendar,
so those must stay execution-disabled until broker capability is verified.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal

from apps.api.app.options.pricing import OptionRight, theoretical_price

CONTRACT_MULTIPLIER = 100
"""US listed equity options: one contract controls 100 shares."""


class StructureKind(str, enum.Enum):  # noqa: UP042
    BULL_CALL = "bull_call"      # debit, bullish
    BEAR_PUT = "bear_put"        # debit, bearish
    BULL_PUT = "bull_put"        # credit, bullish
    BEAR_CALL = "bear_call"      # credit, bearish
    IRON_CONDOR = "iron_condor"  # credit, neutral (non-native execution)


def _money(x: float) -> Decimal:
    """One place converts the float model to money, quantized to cents."""
    return Decimal(str(x)).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class Leg:
    right: OptionRight
    strike: Decimal
    is_long: bool
    modeled_price: Decimal


@dataclass(frozen=True)
class VerticalSpread:
    """A two-leg, single-expiry, single-right defined-risk spread.

    `net_premium` is signed from the ACCOUNT's view: positive = a debit
    paid out, negative = a credit received. `max_loss` and `max_profit` are
    always non-negative magnitudes in account currency for one contract
    (i.e. already ×100).
    """

    kind: StructureKind
    long_leg: Leg
    short_leg: Leg
    net_premium: Decimal
    max_profit: Decimal
    max_loss: Decimal
    breakeven: Decimal
    is_debit: bool

    @property
    def width(self) -> Decimal:
        return abs(self.long_leg.strike - self.short_leg.strike)

    def payoff_at_expiry(self, underlying: Decimal) -> Decimal:
        """Net P&L for ONE contract if held to expiry with the underlying
        settling at `underlying`. Intrinsic only — expiry has no time
        value, so this is exact rather than modeled.
        """
        long_intrinsic = _intrinsic(self.long_leg, underlying)
        short_intrinsic = _intrinsic(self.short_leg, underlying)
        gross = (long_intrinsic - short_intrinsic) * CONTRACT_MULTIPLIER
        # net_premium already reflects debit paid / credit received.
        return gross - self.net_premium * CONTRACT_MULTIPLIER


def _intrinsic(leg: Leg, underlying: Decimal) -> Decimal:
    if leg.right is OptionRight.CALL:
        return max(underlying - leg.strike, Decimal(0))
    return max(leg.strike - underlying, Decimal(0))


def _price(spot, strike, dte_years, vol, right, r, q) -> Decimal:
    return _money(
        theoretical_price(
            spot=spot,
            strike=float(strike),
            time_to_expiry_years=dte_years,
            volatility=vol,
            right=right,
            risk_free_rate=r,
            dividend_yield=q,
        )
    )


def build_vertical(
    *,
    kind: StructureKind,
    spot: float,
    long_strike: Decimal,
    short_strike: Decimal,
    dte_years: float,
    volatility: float,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
) -> VerticalSpread:
    """Model a vertical from the underlying. Refuses a structure whose loss
    is not bounded — the defined-risk guarantee is enforced here, not left
    to the caller."""
    if kind is StructureKind.BULL_CALL:
        right, is_debit = OptionRight.CALL, True
        if not long_strike < short_strike:
            raise ValueError("Bull call: long strike must be below short strike.")
    elif kind is StructureKind.BEAR_PUT:
        right, is_debit = OptionRight.PUT, True
        if not long_strike > short_strike:
            raise ValueError("Bear put: long strike must be above short strike.")
    elif kind is StructureKind.BULL_PUT:
        right, is_debit = OptionRight.PUT, False
        if not long_strike < short_strike:
            raise ValueError("Bull put: long (protective) strike must be below short strike.")
    elif kind is StructureKind.BEAR_CALL:
        right, is_debit = OptionRight.CALL, False
        if not long_strike > short_strike:
            raise ValueError("Bear call: long (protective) strike must be above short strike.")
    else:
        raise ValueError(f"{kind} is not a vertical.")

    long_price = _price(
        spot, long_strike, dte_years, volatility, right, risk_free_rate, dividend_yield
    )
    short_price = _price(
        spot, short_strike, dte_years, volatility, right, risk_free_rate, dividend_yield
    )

    # net_premium > 0 means the account pays (debit); < 0 means it collects.
    net_premium = long_price - short_price
    width = abs(long_strike - short_strike)

    if is_debit:
        # A debit spread must cost something and never more than its width.
        max_loss = net_premium * CONTRACT_MULTIPLIER
        max_profit = (width - net_premium) * CONTRACT_MULTIPLIER
    else:
        credit = -net_premium
        max_profit = credit * CONTRACT_MULTIPLIER
        max_loss = (width - credit) * CONTRACT_MULTIPLIER

    if max_loss <= 0 or max_profit <= 0:
        # The model produced a structure that is not genuinely defined-risk
        # (e.g. a credit exceeding the width, which arbitrage forbids and a
        # model can only produce from bad inputs). Refuse it rather than
        # size a trade against a nonsensical max-loss.
        raise ValueError(
            f"Modeled {kind.value} is not defined-risk "
            f"(max_profit={max_profit}, max_loss={max_loss}); check inputs."
        )

    breakeven = _breakeven(kind, long_strike, short_strike, net_premium)

    return VerticalSpread(
        kind=kind,
        long_leg=Leg(right=right, strike=long_strike, is_long=True, modeled_price=long_price),
        short_leg=Leg(right=right, strike=short_strike, is_long=False, modeled_price=short_price),
        net_premium=net_premium,
        max_profit=max_profit,
        max_loss=max_loss,
        breakeven=breakeven,
        is_debit=is_debit,
    )


def _breakeven(kind, long_strike, short_strike, net_premium) -> Decimal:
    if kind is StructureKind.BULL_CALL:
        return long_strike + net_premium
    if kind is StructureKind.BEAR_PUT:
        return long_strike - net_premium
    if kind is StructureKind.BULL_PUT:
        # credit = -net_premium; breakeven = short put strike - credit
        return short_strike - (-net_premium)
    # BEAR_CALL: breakeven = short call strike + credit
    return short_strike + (-net_premium)


def contracts_for_risk(*, account_risk: Decimal, spread: VerticalSpread) -> int:
    """`floor(account_risk / max_loss_per_contract)` — the playbook's sizing
    formula. Defined-risk means max_loss is the exact denominator, so this
    cannot under-state risk the way an underlying-stop estimate can."""
    if spread.max_loss <= 0:
        return 0
    return int(account_risk // spread.max_loss)
