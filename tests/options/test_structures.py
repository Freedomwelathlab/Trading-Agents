"""Defined-risk structure tests (Phase 75, D093).

These pin the max-loss / max-profit / breakeven math and the defined-risk
refusal, because those figures are the denominator of position sizing — a
wrong max-loss silently mis-sizes the trade.
"""

from decimal import Decimal

import pytest

from apps.api.app.options.structures import (
    CONTRACT_MULTIPLIER,
    StructureKind,
    VerticalSpread,
    build_vertical,
    contracts_for_risk,
)


def test_bull_call_debit_math_is_consistent():
    s = build_vertical(
        kind=StructureKind.BULL_CALL,
        spot=100,
        long_strike=Decimal("100"),
        short_strike=Decimal("105"),
        dte_years=0.05,
        volatility=0.40,
    )
    assert s.is_debit
    assert s.net_premium > 0  # you pay a debit
    # For a debit vertical: max_loss + max_profit == width x 100.
    assert s.max_loss + s.max_profit == pytest.approx(s.width * CONTRACT_MULTIPLIER)
    # Breakeven sits between the strikes for a bull call.
    assert Decimal("100") < s.breakeven < Decimal("105")


def test_bull_put_credit_math_is_consistent():
    s = build_vertical(
        kind=StructureKind.BULL_PUT,
        spot=100,
        long_strike=Decimal("95"),   # protective (bought) lower put
        short_strike=Decimal("100"),  # sold higher put
        dte_years=0.05,
        volatility=0.40,
    )
    assert not s.is_debit
    assert s.net_premium < 0  # you collect a credit
    # For a credit vertical: max_profit + max_loss == width x 100.
    assert s.max_profit + s.max_loss == pytest.approx(s.width * CONTRACT_MULTIPLIER)


def test_payoff_at_expiry_matches_max_profit_and_max_loss():
    s = build_vertical(
        kind=StructureKind.BULL_CALL,
        spot=100,
        long_strike=Decimal("100"),
        short_strike=Decimal("105"),
        dte_years=0.05,
        volatility=0.40,
    )
    # Above the short strike -> full max profit; below the long strike -> full loss.
    assert s.payoff_at_expiry(Decimal("110")) == pytest.approx(s.max_profit)
    assert s.payoff_at_expiry(Decimal("90")) == pytest.approx(-s.max_loss)


def test_credit_spread_payoff_signs():
    s = build_vertical(
        kind=StructureKind.BEAR_CALL,
        spot=100,
        long_strike=Decimal("110"),   # protective (bought) higher call
        short_strike=Decimal("105"),  # sold lower call
        dte_years=0.05,
        volatility=0.40,
    )
    # Underlying stays below the short call -> keep the whole credit.
    assert s.payoff_at_expiry(Decimal("100")) == pytest.approx(s.max_profit)
    # Underlying blows through the long call -> full defined loss.
    assert s.payoff_at_expiry(Decimal("120")) == pytest.approx(-s.max_loss)


def test_wrong_strike_ordering_is_refused_per_structure():
    with pytest.raises(ValueError, match="below"):
        build_vertical(
            kind=StructureKind.BULL_CALL, spot=100,
            long_strike=Decimal("105"), short_strike=Decimal("100"),
            dte_years=0.05, volatility=0.4,
        )
    with pytest.raises(ValueError, match="above"):
        build_vertical(
            kind=StructureKind.BEAR_PUT, spot=100,
            long_strike=Decimal("95"), short_strike=Decimal("100"),
            dte_years=0.05, volatility=0.4,
        )


def test_every_structure_is_defined_risk():
    """The core hard rule: max loss is always bounded and positive."""
    cases = [
        (StructureKind.BULL_CALL, "100", "105"),
        (StructureKind.BEAR_PUT, "100", "95"),
        (StructureKind.BULL_PUT, "95", "100"),
        (StructureKind.BEAR_CALL, "105", "100"),
    ]
    for kind, long_k, short_k in cases:
        s = build_vertical(
            kind=kind, spot=100, long_strike=Decimal(long_k), short_strike=Decimal(short_k),
            dte_years=0.05, volatility=0.4,
        )
        assert s.max_loss > 0
        assert s.max_loss < s.width * CONTRACT_MULTIPLIER + 1  # bounded by width
        assert isinstance(s, VerticalSpread)


def test_position_sizing_floors_to_whole_contracts():
    s = build_vertical(
        kind=StructureKind.BULL_CALL, spot=100,
        long_strike=Decimal("100"), short_strike=Decimal("105"),
        dte_years=0.05, volatility=0.4,
    )
    # Risk budget of exactly 2.5x the per-contract max loss -> 2 contracts.
    budget = s.max_loss * Decimal("2.5")
    assert contracts_for_risk(account_risk=budget, spread=s) == 2
    # Budget below one contract's max loss -> zero, never a fractional risk.
    assert contracts_for_risk(account_risk=s.max_loss - Decimal("1"), spread=s) == 0


def test_wider_debit_spread_costs_more_but_can_earn_more():
    narrow = build_vertical(
        kind=StructureKind.BULL_CALL, spot=100,
        long_strike=Decimal("100"), short_strike=Decimal("102"),
        dte_years=0.05, volatility=0.4,
    )
    wide = build_vertical(
        kind=StructureKind.BULL_CALL, spot=100,
        long_strike=Decimal("100"), short_strike=Decimal("110"),
        dte_years=0.05, volatility=0.4,
    )
    assert wide.max_loss > narrow.max_loss
    assert wide.max_profit > narrow.max_profit
