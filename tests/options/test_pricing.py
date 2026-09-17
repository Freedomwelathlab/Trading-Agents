"""Black-Scholes model tests (Phase 75, D093).

Anchored to textbook values and to the model's own invariants (put-call
parity, delta bounds, expiry collapse), because a mispriced leg silently
mis-sizes every spread built on it.
"""

import math

import pytest

from apps.api.app.options.pricing import (
    Greeks,
    OptionRight,
    compute_greeks,
    implied_delta_strike,
    theoretical_price,
)


def test_atm_one_year_matches_textbook_value():
    # S=K=100, T=1, sigma=0.20, r=q=0 -> the standard ~7.9656 ATM value.
    price = theoretical_price(
        spot=100, strike=100, time_to_expiry_years=1.0, volatility=0.20, right=OptionRight.CALL
    )
    assert price == pytest.approx(7.9656, abs=1e-3)


def test_put_call_parity_holds():
    # C - P = S*e^-qT - K*e^-rT. With r=q=0 and S=K, calls and puts match.
    kw = dict(spot=100, strike=100, time_to_expiry_years=0.5, volatility=0.30)
    call = theoretical_price(right=OptionRight.CALL, **kw)
    put = theoretical_price(right=OptionRight.PUT, **kw)
    assert call == pytest.approx(put, abs=1e-9)

    # And with a rate, parity still holds exactly.
    kw2 = dict(
        spot=105, strike=100, time_to_expiry_years=0.75, volatility=0.25, risk_free_rate=0.04
    )
    c = theoretical_price(right=OptionRight.CALL, **kw2)
    p = theoretical_price(right=OptionRight.PUT, **kw2)
    parity = 105 - 100 * math.exp(-0.04 * 0.75)
    assert (c - p) == pytest.approx(parity, abs=1e-6)


def test_call_delta_is_between_zero_and_one_and_put_is_negative():
    call = compute_greeks(
        spot=100, strike=100, time_to_expiry_years=1.0, volatility=0.2, right=OptionRight.CALL
    )
    put = compute_greeks(
        spot=100, strike=100, time_to_expiry_years=1.0, volatility=0.2, right=OptionRight.PUT
    )
    assert 0.0 < call.delta < 1.0
    assert -1.0 < put.delta < 0.0
    # ATM one-year call delta is ~0.54, not 0.50, because of the drift term.
    assert call.delta == pytest.approx(0.5398, abs=1e-3)
    # Parity of deltas: delta_call - delta_put = e^{-qT} = 1 here.
    assert call.delta - put.delta == pytest.approx(1.0, abs=1e-9)


def test_gamma_and_vega_are_positive_for_both_rights():
    for right in (OptionRight.CALL, OptionRight.PUT):
        g = compute_greeks(
            spot=100, strike=100, time_to_expiry_years=0.25, volatility=0.3, right=right
        )
        assert g.gamma > 0
        assert g.vega_per_point > 0
        # Long options lose value over time: theta per day is negative.
        assert g.theta_per_day < 0


def test_deeper_in_the_money_call_is_worth_more():
    cheap = theoretical_price(
        spot=100, strike=110, time_to_expiry_years=0.5, volatility=0.25, right=OptionRight.CALL
    )
    dear = theoretical_price(
        spot=100, strike=90, time_to_expiry_years=0.5, volatility=0.25, right=OptionRight.CALL
    )
    assert dear > cheap


def test_at_expiry_price_collapses_to_intrinsic():
    itm = theoretical_price(
        spot=110, strike=100, time_to_expiry_years=0.0, volatility=0.3, right=OptionRight.CALL
    )
    otm = theoretical_price(
        spot=90, strike=100, time_to_expiry_years=0.0, volatility=0.3, right=OptionRight.CALL
    )
    assert itm == pytest.approx(10.0)
    assert otm == pytest.approx(0.0)


def test_zero_volatility_gives_discounted_intrinsic_not_an_error():
    # A degenerate but real backtest input; must not raise.
    price = theoretical_price(
        spot=110, strike=100, time_to_expiry_years=1.0, volatility=0.0,
        right=OptionRight.CALL, risk_free_rate=0.05,
    )
    assert price == pytest.approx(10.0 * math.exp(-0.05), abs=1e-6)


def test_expiry_greeks_report_the_honest_step_not_undefined_numbers():
    g = compute_greeks(
        spot=110, strike=100, time_to_expiry_years=0.0, volatility=0.3, right=OptionRight.CALL
    )
    assert isinstance(g, Greeks)
    assert g.delta == 1.0  # in the money at expiry
    assert g.gamma == 0.0 and g.vega_per_point == 0.0


def test_nonpositive_spot_or_strike_is_refused():
    with pytest.raises(ValueError, match="positive"):
        theoretical_price(
            spot=0, strike=100, time_to_expiry_years=1.0, volatility=0.2, right=OptionRight.CALL
        )


def test_implied_delta_strike_inverts_delta_to_a_strike():
    # A ~0.30-delta call sits above spot; feeding that strike back through
    # the model reproduces ~0.30 delta.
    strike = implied_delta_strike(
        spot=100, target_delta=0.30, time_to_expiry_years=0.1, volatility=0.4,
        right=OptionRight.CALL,
    )
    assert strike > 100
    back = compute_greeks(
        spot=100, strike=strike, time_to_expiry_years=0.1, volatility=0.4, right=OptionRight.CALL
    )
    assert abs(back.delta) == pytest.approx(0.30, abs=1e-3)


def test_implied_delta_strike_for_put_sits_below_spot():
    strike = implied_delta_strike(
        spot=100, target_delta=0.30, time_to_expiry_years=0.1, volatility=0.4, right=OptionRight.PUT
    )
    assert strike < 100
    back = compute_greeks(
        spot=100, strike=strike, time_to_expiry_years=0.1, volatility=0.4, right=OptionRight.PUT
    )
    assert abs(back.delta) == pytest.approx(0.30, abs=1e-3)


def test_target_delta_must_be_a_magnitude_in_range():
    with pytest.raises(ValueError, match="magnitude"):
        implied_delta_strike(
            spot=100, target_delta=1.5, time_to_expiry_years=0.1, volatility=0.3,
            right=OptionRight.CALL,
        )
