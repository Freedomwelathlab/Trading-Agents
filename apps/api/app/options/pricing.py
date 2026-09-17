"""Black-Scholes option pricing and Greeks (Phase 75, D093).

**Everything this module produces is a MODEL, not a market quote, and the
whole options subsystem is built around keeping that distinction visible.**

Trading OS has no historical option-chain data — Longbridge exposes live
option quotes and Greeks (with OPRA permission) for forward/paper use, but
nothing supplies past option prices. A backtest of an option spread over
historical *underlying* bars therefore has to SYNTHESIZE each leg's price
from the underlying with a pricing model. Black-Scholes is that model. The
values it returns are theoretical, they assume a volatility that itself has
to be supplied or assumed, and they will differ from what a real chain
would have quoted — most of all for short-dated, low-liquidity contracts,
which is exactly where the playbook's 0DTE/1-3DTE tactics live.

So: these functions are named `theoretical_*`, every result carries no
pretence of being a fill, and the backtest layer that consumes them stamps
its runs as model-priced. Live/paper execution must use the vendor's real
option quotes, never this module. The anti-fabrication rule
(docs/TRADING_SAFETY.md) is the reason for all of that.

The math is standard European Black-Scholes-Merton with a continuous
dividend yield `q`. Floats are used throughout: BS is a floating-point
model and pretending otherwise with `Decimal` would imply a precision the
model does not have. Money figures are converted to `Decimal` only at the
structure boundary (see structures.py), where they represent an actual
premium or max-loss a position would carry.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass


class OptionRight(str, enum.Enum):  # noqa: UP042 (str mixin matches this codebase)
    CALL = "call"
    PUT = "put"


@dataclass(frozen=True)
class Greeks:
    """Per-contract-share sensitivities of the theoretical price.

    `theta` and `vega` are scaled to the units a trader reads: theta is per
    CALENDAR DAY (not per year), vega is per ONE VOLATILITY POINT (a change
    of 0.01 in sigma), because that is how every options screen quotes them
    and mixing the conventions is a common, silent error.
    """

    price: float
    delta: float
    gamma: float
    theta_per_day: float
    vega_per_point: float


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _intrinsic(spot: float, strike: float, right: OptionRight) -> float:
    if right is OptionRight.CALL:
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def theoretical_price(
    *,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    volatility: float,
    right: OptionRight,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
) -> float:
    """Theoretical European option price.

    At or past expiry, or with a non-positive volatility, the model
    collapses to discounted intrinsic value rather than raising: a backtest
    routinely evaluates a position on its expiry bar, and that boundary is a
    real value, not an error.
    """
    if strike <= 0 or spot <= 0:
        raise ValueError("spot and strike must be positive.")
    if time_to_expiry_years <= 0 or volatility <= 0:
        # Discounted intrinsic — the only defensible value with no time or
        # no diffusion left.
        discount = math.exp(-risk_free_rate * max(time_to_expiry_years, 0.0))
        return _intrinsic(spot, strike, right) * discount

    greeks = compute_greeks(
        spot=spot,
        strike=strike,
        time_to_expiry_years=time_to_expiry_years,
        volatility=volatility,
        right=right,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
    )
    return greeks.price


def compute_greeks(
    *,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    volatility: float,
    right: OptionRight,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
) -> Greeks:
    """Price and Greeks together, since they share `d1`/`d2` and computing
    them separately would evaluate the same normal functions twice."""
    if strike <= 0 or spot <= 0:
        raise ValueError("spot and strike must be positive.")

    if time_to_expiry_years <= 0 or volatility <= 0:
        price = theoretical_price(
            spot=spot,
            strike=strike,
            time_to_expiry_years=time_to_expiry_years,
            volatility=volatility,
            right=right,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
        )
        # A position at expiry has delta ±1 in the money and 0 out; the
        # continuous Greeks are undefined there, so report the honest step.
        itm = _intrinsic(spot, strike, right) > 0
        delta = (1.0 if right is OptionRight.CALL else -1.0) if itm else 0.0
        return Greeks(price=price, delta=delta, gamma=0.0, theta_per_day=0.0, vega_per_point=0.0)

    t = time_to_expiry_years
    vol_sqrt_t = volatility * math.sqrt(t)
    d1 = (
        math.log(spot / strike) + (risk_free_rate - dividend_yield + 0.5 * volatility**2) * t
    ) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t

    disc_r = math.exp(-risk_free_rate * t)
    disc_q = math.exp(-dividend_yield * t)

    if right is OptionRight.CALL:
        price = spot * disc_q * _norm_cdf(d1) - strike * disc_r * _norm_cdf(d2)
        delta = disc_q * _norm_cdf(d1)
        theta_year = (
            -(spot * disc_q * _norm_pdf(d1) * volatility) / (2 * math.sqrt(t))
            - risk_free_rate * strike * disc_r * _norm_cdf(d2)
            + dividend_yield * spot * disc_q * _norm_cdf(d1)
        )
    else:
        price = strike * disc_r * _norm_cdf(-d2) - spot * disc_q * _norm_cdf(-d1)
        delta = -disc_q * _norm_cdf(-d1)
        theta_year = (
            -(spot * disc_q * _norm_pdf(d1) * volatility) / (2 * math.sqrt(t))
            + risk_free_rate * strike * disc_r * _norm_cdf(-d2)
            - dividend_yield * spot * disc_q * _norm_cdf(-d1)
        )

    gamma = (disc_q * _norm_pdf(d1)) / (spot * vol_sqrt_t)
    vega_year = spot * disc_q * _norm_pdf(d1) * math.sqrt(t)

    return Greeks(
        price=price,
        delta=delta,
        gamma=gamma,
        theta_per_day=theta_year / 365.0,
        vega_per_point=vega_year / 100.0,
    )


def implied_delta_strike(
    *,
    spot: float,
    target_delta: float,
    time_to_expiry_years: float,
    volatility: float,
    right: OptionRight,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
    search_span: float = 0.6,
) -> float:
    """The strike whose theoretical delta is closest to `target_delta`.

    The playbook selects legs by delta ("long leg 0.55-0.65, short leg
    0.20-0.35"), but a chain is quoted by strike, so this inverts the
    relationship. `target_delta` is given as a MAGNITUDE (0-1); the sign is
    taken from the right, so a 0.30-delta put and a 0.30-delta call are both
    asked for as 0.30.

    A monotone bisection on strike — delta is monotone in strike for a
    vanilla option, so this converges without the pathologies a
    Newton step can hit near expiry. Returns a strike, not a contract:
    rounding to the chain's real strike increments is the selector's job,
    against real quotes, not the model's.
    """
    if not 0.0 < target_delta < 1.0:
        raise ValueError("target_delta must be a magnitude strictly between 0 and 1.")

    def delta_at(strike: float) -> float:
        return abs(
            compute_greeks(
                spot=spot,
                strike=strike,
                time_to_expiry_years=time_to_expiry_years,
                volatility=volatility,
                right=right,
                risk_free_rate=risk_free_rate,
                dividend_yield=dividend_yield,
            ).delta
        )

    # Delta falls as a call strike rises (and as a put strike falls), so
    # bracket wide and bisect. The span is a fraction of spot.
    low, high = spot * (1 - search_span), spot * (1 + search_span)
    for _ in range(100):
        mid = 0.5 * (low + high)
        dmid = delta_at(mid)
        if abs(dmid - target_delta) < 1e-5:
            return mid
        # For a call, higher strike -> lower delta; for a put, higher
        # strike -> higher |delta|.
        rising = (dmid < target_delta) == (right is OptionRight.CALL)
        if rising:
            high = mid
        else:
            low = mid
    return 0.5 * (low + high)
