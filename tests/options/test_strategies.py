"""Option strategy-layer tests (Phase 78, D096).

These pin the four rules the playbook states as mandatory — the §5 score,
the §16 score floor, the §10A path split, and the §3 sizing tiers — plus
the strike ordering each structure needs. A strategy layer that quietly
relaxed any of them would still return plausible-looking plans, which is
exactly why each gets an explicit test.
"""

from decimal import Decimal

import pytest

from apps.api.app.options.selection import DteBand
from apps.api.app.options.strategies import (
    MAX_SCORE,
    MINIMUM_TRADEABLE_SCORE,
    RISK_A_PLUS,
    RISK_NORMAL,
    RISK_SPECULATIVE,
    OptionTradePlan,
    SignalEvidence,
    SignalGrade,
    StrategyPath,
    StructureKind,
    TradeRefusal,
    grade_score,
    plan_option_trade,
    risk_budget_pct,
    route_path,
    score_signal,
    select_strikes,
)

EQUITY = Decimal("100000")


def evidence(**overrides) -> SignalEvidence:
    """A-grade by default; tests knock individual pieces out."""
    base = dict(
        liquidity_sweep=True, market_structure_shift=True, cross_market_confirm=True,
        vwap_location=True, rsi_divergence=True, volume_confirm=True,
        atr_confirm=True, major_level=True, option_liquidity_ok=True,
        iv_appropriate=True,
    )
    base.update(overrides)
    return SignalEvidence(**base)


# --- §5 scoring ------------------------------------------------------------


def test_full_evidence_scores_twelve_and_nothing_scores_zero():
    assert score_signal(evidence()) == MAX_SCORE == 12
    assert score_signal(SignalEvidence()) == 0


def test_sweep_and_mss_are_worth_two_each_and_the_rest_one():
    assert score_signal(SignalEvidence(liquidity_sweep=True)) == 2
    assert score_signal(SignalEvidence(market_structure_shift=True)) == 2
    assert score_signal(SignalEvidence(rsi_divergence=True)) == 1


def test_grade_boundaries_match_the_playbook_table():
    assert grade_score(12) is SignalGrade.A_PLUS
    assert grade_score(10) is SignalGrade.A_PLUS
    assert grade_score(9) is SignalGrade.CANDIDATE
    assert grade_score(8) is SignalGrade.CANDIDATE
    assert grade_score(7) is SignalGrade.WATCHLIST
    assert grade_score(5) is SignalGrade.NO_TRADE


# --- §16 hard floor --------------------------------------------------------


def test_a_watchlist_grade_signal_is_refused_not_traded():
    """7/12 is 'watchlist' in the playbook's own table, and §16 makes that a
    refusal. A layer that traded it would be silently overriding the rule."""
    # 8 is the floor and IS tradeable; one point less is not.
    at_floor = SignalEvidence(liquidity_sweep=True, market_structure_shift=True,
                              vwap_location=True, major_level=True,
                              option_liquidity_ok=True, iv_appropriate=True)
    assert score_signal(at_floor) == MINIMUM_TRADEABLE_SCORE == 8
    weaker = SignalEvidence(liquidity_sweep=True, market_structure_shift=True,
                            vwap_location=True, major_level=True,
                            iv_appropriate=True)
    assert score_signal(weaker) == 7

    result = plan_option_trade(
        bullish=True, evidence=weaker, spot=100.0, dte_days=7,
        volatility=0.45, iv_rank=0.3, account_equity=EQUITY,
    )
    assert isinstance(result, TradeRefusal)
    assert str(MINIMUM_TRADEABLE_SCORE) in result.reason
    assert result.grade is SignalGrade.WATCHLIST


# --- §10A router -----------------------------------------------------------


def test_a_directional_edge_beats_a_premium_condition():
    """Both conditions true: the playbook keeps reversal as the primary path
    and forbids opening an opposing premium position at the same level."""
    assert route_path(has_directional_edge=True, is_range_bound=True,
                      iv_rank=0.9, directional_score=10) is StrategyPath.REVERSAL


def test_a_range_with_rich_iv_routes_to_premium():
    assert route_path(has_directional_edge=False, is_range_bound=True,
                      iv_rank=0.7, directional_score=10) is StrategyPath.PREMIUM


def test_a_range_with_cheap_iv_is_no_trade():
    """Selling premium is only paid for when premium is rich."""
    assert route_path(has_directional_edge=False, is_range_bound=True,
                      iv_rank=0.2, directional_score=10) is StrategyPath.NO_TRADE


def test_a_weak_directional_read_does_not_claim_the_reversal_path():
    assert route_path(has_directional_edge=True, is_range_bound=False,
                      iv_rank=0.3, directional_score=6) is StrategyPath.NO_TRADE


# --- §3 sizing -------------------------------------------------------------


def test_risk_tiers_follow_grade():
    assert risk_budget_pct(SignalGrade.A_PLUS, DteBand.REGULAR) == RISK_A_PLUS
    assert risk_budget_pct(SignalGrade.CANDIDATE, DteBand.REGULAR) == RISK_NORMAL


def test_zero_dte_is_capped_at_the_speculative_tier_even_for_an_a_plus():
    """The playbook gives 0DTE its own ceiling; a perfect score must not
    unlock a bigger 0DTE position."""
    assert risk_budget_pct(SignalGrade.A_PLUS, DteBand.ZERO_DTE) == RISK_SPECULATIVE
    assert RISK_SPECULATIVE < RISK_A_PLUS


# --- strikes ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "long_below"),
    [
        (StructureKind.BULL_CALL, True),
        (StructureKind.BEAR_PUT, False),
        (StructureKind.BULL_PUT, True),
        (StructureKind.BEAR_CALL, False),
    ],
)
def test_every_vertical_gets_the_strike_ordering_it_requires(kind, long_below):
    long_k, short_k = select_strikes(
        kind=kind, spot=100.0, dte_years=7 / 365, volatility=0.45,
    )
    assert long_k != short_k
    assert (long_k < short_k) is long_below
    # And the ordering is exactly what build_vertical will accept.
    from apps.api.app.options.structures import build_vertical

    build_vertical(kind=kind, spot=100.0, long_strike=long_k, short_strike=short_k,
                   dte_years=7 / 365, volatility=0.45)


def test_strikes_snap_to_the_chain_increment():
    long_k, short_k = select_strikes(
        kind=StructureKind.BULL_CALL, spot=100.0, dte_years=7 / 365,
        volatility=0.45, strike_increment=Decimal("5"),
    )
    assert long_k % 5 == 0 and short_k % 5 == 0


# --- end to end ------------------------------------------------------------


def test_a_plus_bullish_reversal_produces_a_defined_risk_debit_call_spread():
    plan = plan_option_trade(
        bullish=True, evidence=evidence(), spot=100.0, dte_days=7,
        volatility=0.45, iv_rank=0.30, account_equity=EQUITY,
    )
    assert isinstance(plan, OptionTradePlan)
    assert plan.path is StrategyPath.REVERSAL
    assert plan.spread.kind is StructureKind.BULL_CALL
    assert plan.spread.is_debit
    assert plan.grade is SignalGrade.A_PLUS
    assert plan.contracts >= 1
    # The whole point of a defined-risk structure: total loss cannot exceed
    # the §3 budget it was sized against.
    assert plan.max_loss_total <= plan.risk_budget
    assert plan.risk_budget == EQUITY * RISK_A_PLUS
    assert "MODEL" in plan.notes["priced"]


def test_a_bearish_reversal_produces_a_put_debit_spread():
    plan = plan_option_trade(
        bullish=False, evidence=evidence(), spot=100.0, dte_days=7,
        volatility=0.45, iv_rank=0.30, account_equity=EQUITY,
    )
    assert isinstance(plan, OptionTradePlan)
    assert plan.spread.kind is StructureKind.BEAR_PUT


def test_the_premium_path_produces_a_credit_spread_never_a_naked_short():
    plan = plan_option_trade(
        bullish=True, evidence=evidence(), spot=100.0, dte_days=7,
        volatility=0.45, iv_rank=0.80, account_equity=EQUITY,
        has_directional_edge=False, is_range_bound=True,
    )
    assert isinstance(plan, OptionTradePlan)
    assert plan.path is StrategyPath.PREMIUM
    assert plan.spread.kind is StructureKind.BULL_PUT
    assert not plan.spread.is_debit
    # The protective long leg is what makes it defined-risk.
    assert plan.spread.long_leg.is_long
    assert plan.spread.max_loss > 0


def test_buying_debit_premium_into_rich_iv_is_refused():
    """§2: a debit structure in a high-IV regime is the wrong side of the
    volatility trade, so the router's choice and the IV filter must agree."""
    result = plan_option_trade(
        bullish=True, evidence=evidence(), spot=100.0, dte_days=7,
        volatility=0.45, iv_rank=0.90, account_equity=EQUITY,
    )
    assert isinstance(result, TradeRefusal)
    assert "debit" in result.reason


def test_a_dte_beyond_the_playbooks_bands_is_refused():
    result = plan_option_trade(
        bullish=True, evidence=evidence(), spot=100.0, dte_days=45,
        volatility=0.45, iv_rank=0.30, account_equity=EQUITY,
    )
    assert isinstance(result, TradeRefusal)
    assert "DTE" in result.reason


def test_an_account_too_small_for_one_contract_refuses_rather_than_rounding_up():
    """Sizing to zero is the correct answer for an account that cannot
    afford the structure — rounding up to one contract would exceed the
    risk budget the same rules just computed."""
    result = plan_option_trade(
        bullish=True, evidence=evidence(), spot=100.0, dte_days=7,
        volatility=0.45, iv_rank=0.30, account_equity=Decimal("500"),
    )
    assert isinstance(result, TradeRefusal)
    assert "one contract" in result.reason


def test_zero_dte_sizes_smaller_than_the_same_signal_at_seven_dte():
    """The tier difference must show up in contracts, not just in a label."""
    a = plan_option_trade(bullish=True, evidence=evidence(), spot=100.0,
                          dte_days=7, volatility=0.45, iv_rank=0.30,
                          account_equity=EQUITY)
    b = plan_option_trade(bullish=True, evidence=evidence(), spot=100.0,
                          dte_days=0, volatility=0.45, iv_rank=0.30,
                          account_equity=EQUITY)
    assert isinstance(a, OptionTradePlan) and isinstance(b, OptionTradePlan)
    assert b.dte_band is DteBand.ZERO_DTE
    assert b.risk_budget < a.risk_budget


def test_a_zero_dte_plan_with_no_session_time_left_is_an_error_not_a_zero_risk_trade():
    """A literal zero time to expiry collapses the model's max-loss to 0,
    which would size an unbounded number of contracts. Refuse to model it."""
    with pytest.raises(ValueError, match="hours still remaining"):
        plan_option_trade(
            bullish=True, evidence=evidence(), spot=100.0, dte_days=0,
            volatility=0.45, iv_rank=0.30, account_equity=EQUITY,
            zero_dte_hours_remaining=0.0,
        )
