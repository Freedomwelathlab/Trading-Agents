"""Pure unit tests for the two Phase-62 market-risk constraints on the
trade-path Portfolio Manager (D079). No DB, no HTTP - `MarketRiskInputs` is
built directly from hand-chosen volatilities and correlations, exactly the
way `decide()`'s trade-path caller hands it in.

The three D029 constraints already have their own file
(`test_manager.py`); this one only covers what `market_risk` adds.
"""

from datetime import UTC, datetime
from decimal import Decimal

from apps.api.app.portfolio_manager.manager import decide
from apps.api.app.portfolio_manager.models import (
    MarketRiskInputs,
    PortfolioAction,
    PortfolioCheck,
    PortfolioConstraint,
    PortfolioHolding,
    PortfolioLimits,
    PortfolioState,
)
from apps.api.app.risk.models import Side, TradeProposal

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)

_D029_CONSTRAINTS = {
    PortfolioConstraint.SYMBOL_CONCENTRATION,
    PortfolioConstraint.CASH_RESERVE,
    PortfolioConstraint.MAX_OPEN_POSITIONS,
}


def make_proposal(**overrides) -> TradeProposal:
    defaults = dict(
        symbol="X",
        side=Side.BUY,
        quantity=Decimal(100),
        estimated_price=Decimal(100),
        stop_price=Decimal(95),
        market_data_as_of=NOW,
    )
    defaults.update(overrides)
    return TradeProposal(**defaults)


def make_limits(**overrides) -> PortfolioLimits:
    """Only the three D029 fields are set by default, so no market-risk
    check runs unless a test opts in - the state the trade path is in when
    `config.py` has the limit but the test omits it."""
    defaults = dict(
        max_symbol_pct_of_equity=Decimal(1),
        min_cash_reserve_pct_of_equity=Decimal(0),
        max_open_positions=50,
    )
    defaults.update(overrides)
    return PortfolioLimits(**defaults)


def _risk(**overrides) -> MarketRiskInputs:
    defaults: dict = dict(
        annualized_volatility={"H": Decimal("0.20"), "X": Decimal("0.40")},
        correlation={MarketRiskInputs._pair_key("H", "X"): Decimal("0.5")},
        lookback_days=365,
        min_observations=60,
    )
    defaults.update(overrides)
    return MarketRiskInputs(**defaults)


def _held_h() -> PortfolioState:
    """One held name H worth $10k, $10k cash - equity $20k."""
    return PortfolioState(
        cash=Decimal(10_000),
        holdings=[
            PortfolioHolding(
                symbol="H", quantity=Decimal(100), market_value=Decimal(10_000)
            )
        ],
    )


def _check(decision, constraint) -> PortfolioCheck:
    return next(c for c in decision.checks if c.constraint is constraint)


# --------------------------------------------------------------------------
# market_risk not supplied / limits not set
# --------------------------------------------------------------------------


def test_without_market_risk_decide_returns_exactly_the_three_d029_checks():
    decision = decide(make_proposal(quantity=Decimal(10)), _held_h(), make_limits())

    assert {c.constraint for c in decision.checks} == _D029_CONSTRAINTS
    assert decision.action is PortfolioAction.APPROVE


def test_market_risk_supplied_but_limits_unset_still_runs_only_the_three_checks():
    decision = decide(
        make_proposal(quantity=Decimal(10)), _held_h(), make_limits(), _risk()
    )

    assert {c.constraint for c in decision.checks} == _D029_CONSTRAINTS


def test_only_the_volatility_limit_set_adds_only_the_volatility_check():
    decision = decide(
        make_proposal(quantity=Decimal(10)),
        _held_h(),
        make_limits(max_portfolio_volatility_pct=Decimal("0.90")),
        _risk(),
    )
    got = {c.constraint for c in decision.checks}
    assert PortfolioConstraint.PORTFOLIO_VOLATILITY in got
    assert PortfolioConstraint.POSITION_CORRELATION not in got


def test_only_the_correlation_limit_set_adds_only_the_correlation_check():
    decision = decide(
        make_proposal(quantity=Decimal(10)),
        _held_h(),
        make_limits(max_position_correlation=Decimal("0.99")),
        _risk(),
    )
    got = {c.constraint for c in decision.checks}
    assert PortfolioConstraint.POSITION_CORRELATION in got
    assert PortfolioConstraint.PORTFOLIO_VOLATILITY not in got


# --------------------------------------------------------------------------
# PORTFOLIO_VOLATILITY - clean pass / fail on hand-computable weights
# --------------------------------------------------------------------------


def _expected_two_asset_vol(
    w1: Decimal, s1: Decimal, w2: Decimal, s2: Decimal, rho: Decimal
) -> Decimal:
    return (
        w1 * w1 * s1 * s1
        + w2 * w2 * s2 * s2
        + 2 * w1 * w2 * s1 * s2 * rho
    ).sqrt()


def test_volatility_check_passes_and_reports_the_sqrt_w_sigma_form():
    # Buy 10 X @ 100 = $1,000 -> w_X = 1000/20000 = 0.05; w_H = 0.5.
    decision = decide(
        make_proposal(quantity=Decimal(10)),
        _held_h(),
        make_limits(max_portfolio_volatility_pct=Decimal("0.15")),
        _risk(),
    )
    vol = _check(decision, PortfolioConstraint.PORTFOLIO_VOLATILITY)
    expected = _expected_two_asset_vol(
        Decimal("0.5"), Decimal("0.20"), Decimal("0.05"), Decimal("0.40"), Decimal("0.5")
    )
    assert abs(vol.projected_value - expected) < Decimal("1e-24")
    assert vol.passed is True
    assert decision.action is PortfolioAction.APPROVE


def test_volatility_check_fails_cleanly_when_the_book_would_breach_the_cap():
    # Buy 100 X @ 100 -> w_X = w_H = 0.5; projected vol = sqrt(0.07).
    decision = decide(
        make_proposal(quantity=Decimal(100)),
        _held_h(),
        make_limits(max_portfolio_volatility_pct=Decimal("0.15")),
        _risk(),
    )
    vol = _check(decision, PortfolioConstraint.PORTFOLIO_VOLATILITY)
    assert abs(vol.projected_value - Decimal("0.07").sqrt()) < Decimal("1e-24")
    assert vol.passed is False
    assert vol.worsened_by_trade is True


def test_volatility_binding_sizes_the_buy_down_to_the_bisection_boundary():
    limits = make_limits(max_portfolio_volatility_pct=Decimal("0.15"))
    risk = _risk()
    portfolio = _held_h()

    decision = decide(make_proposal(quantity=Decimal(100)), portfolio, limits, risk)
    assert decision.action is PortfolioAction.MODIFY
    assert decision.binding_constraint is PortfolioConstraint.PORTFOLIO_VOLATILITY
    approved = decision.approved_quantity

    # The approved quantity's projected vol is <= the cap, and one more
    # share tips it over - the boundary is exact for integer quantities.
    at_approved = _check(
        decide(make_proposal(quantity=approved), portfolio, limits, risk),
        PortfolioConstraint.PORTFOLIO_VOLATILITY,
    )
    at_next = _check(
        decide(make_proposal(quantity=approved + 1), portfolio, limits, risk),
        PortfolioConstraint.PORTFOLIO_VOLATILITY,
    )
    assert at_approved.projected_value <= Decimal("0.15")
    assert at_next.projected_value > Decimal("0.15")


def test_a_de_risking_sell_that_lowers_projected_volatility_is_never_blocked():
    portfolio = PortfolioState(
        cash=Decimal(1_000),
        holdings=[
            PortfolioHolding(
                symbol="H", quantity=Decimal(100), market_value=Decimal(10_000)
            ),
            PortfolioHolding(
                symbol="X", quantity=Decimal(100), market_value=Decimal(10_000)
            ),
        ],
    )
    risk = _risk(
        annualized_volatility={"H": Decimal("0.20"), "X": Decimal("0.60")},
        correlation={MarketRiskInputs._pair_key("H", "X"): Decimal("0.70")},
    )
    # An absurdly tight cap the book already breaches - the sell still must
    # not be refused, because it relieves the breach.
    decision = decide(
        make_proposal(symbol="X", side=Side.SELL, quantity=Decimal(50)),
        portfolio,
        make_limits(max_portfolio_volatility_pct=Decimal("0.001")),
        risk,
    )
    vol = _check(decision, PortfolioConstraint.PORTFOLIO_VOLATILITY)
    assert vol.passed is False  # still over the (absurd) cap
    assert vol.worsened_by_trade is False  # but the sell improved it
    assert decision.action is PortfolioAction.APPROVE


# --------------------------------------------------------------------------
# POSITION_CORRELATION - opening only, REJECT not MODIFY
# --------------------------------------------------------------------------


def test_opening_a_highly_correlated_new_position_is_rejected_not_modified():
    risk = _risk(
        annualized_volatility={"H": Decimal("0.20"), "X": Decimal("0.20")},
        correlation={MarketRiskInputs._pair_key("H", "X"): Decimal("0.90")},
    )
    decision = decide(
        make_proposal(symbol="X", quantity=Decimal(10)),
        _held_h(),
        make_limits(max_position_correlation=Decimal("0.80")),
        risk,
    )
    assert decision.action is PortfolioAction.REJECT
    assert decision.binding_constraint is PortfolioConstraint.POSITION_CORRELATION
    assert decision.approved_quantity == Decimal(0)
    corr = _check(decision, PortfolioConstraint.POSITION_CORRELATION)
    assert corr.passed is False
    assert corr.worsened_by_trade is True


def test_adding_to_an_already_held_correlated_position_is_not_blocked():
    portfolio = PortfolioState(
        cash=Decimal(50_000),
        holdings=[
            PortfolioHolding(
                symbol="H", quantity=Decimal(100), market_value=Decimal(10_000)
            ),
            PortfolioHolding(
                symbol="G", quantity=Decimal(100), market_value=Decimal(10_000)
            ),
        ],
    )
    risk = _risk(
        annualized_volatility={"H": Decimal("0.20"), "G": Decimal("0.20")},
        correlation={MarketRiskInputs._pair_key("G", "H"): Decimal("0.95")},
    )
    decision = decide(
        make_proposal(symbol="H", quantity=Decimal(10)),
        portfolio,
        make_limits(max_position_correlation=Decimal("0.80")),
        risk,
    )
    corr = _check(decision, PortfolioConstraint.POSITION_CORRELATION)
    assert corr.passed is False  # H really is 0.95 correlated with G
    assert corr.worsened_by_trade is False  # but adding to a held name can't breach it
    assert decision.action is PortfolioAction.APPROVE


def test_correlation_check_passes_trivially_with_no_other_holdings():
    portfolio = PortfolioState(cash=Decimal(20_000), holdings=[])
    decision = decide(
        make_proposal(symbol="X", quantity=Decimal(10)),
        portfolio,
        make_limits(max_position_correlation=Decimal("0.80")),
        _risk(),
    )
    corr = _check(decision, PortfolioConstraint.POSITION_CORRELATION)
    assert corr.passed is True
    assert corr.worsened_by_trade is False
    assert "No other position" in corr.detail
    assert decision.action is PortfolioAction.APPROVE


# --------------------------------------------------------------------------
# SKIP paths - thin bar history, audited but non-binding
# --------------------------------------------------------------------------


def test_volatility_check_skips_when_a_held_symbol_is_uncovered():
    # H (held, weighted) is absent from annualized_volatility.
    risk = _risk(annualized_volatility={"X": Decimal("0.40")}, correlation={})
    decision = decide(
        make_proposal(symbol="X", quantity=Decimal(10)),
        _held_h(),
        make_limits(max_portfolio_volatility_pct=Decimal("0.05")),
        risk,
    )
    vol = _check(decision, PortfolioConstraint.PORTFOLIO_VOLATILITY)
    assert vol.skipped is True
    assert vol.passed is False
    assert vol.worsened_by_trade is False
    # Not binding - the trade proceeds on the D029 checks.
    assert decision.action is PortfolioAction.APPROVE


def test_correlation_check_skips_when_the_proposed_symbol_is_uncovered():
    risk = _risk(annualized_volatility={"H": Decimal("0.20")}, correlation={})
    decision = decide(
        make_proposal(symbol="X", quantity=Decimal(10)),
        _held_h(),
        make_limits(max_position_correlation=Decimal("0.10")),
        risk,
    )
    corr = _check(decision, PortfolioConstraint.POSITION_CORRELATION)
    assert corr.skipped is True
    assert corr.passed is False
    assert corr.worsened_by_trade is False
    assert decision.action is PortfolioAction.APPROVE


def test_correlation_check_skips_when_every_pair_correlation_is_missing():
    # Both symbols covered for volatility, but no correlation entry exists.
    risk = _risk(
        annualized_volatility={"H": Decimal("0.20"), "X": Decimal("0.20")},
        correlation={},
    )
    decision = decide(
        make_proposal(symbol="X", quantity=Decimal(10)),
        _held_h(),
        make_limits(max_position_correlation=Decimal("0.10")),
        risk,
    )
    corr = _check(decision, PortfolioConstraint.POSITION_CORRELATION)
    assert corr.skipped is True
    assert decision.action is PortfolioAction.APPROVE


# --------------------------------------------------------------------------
# tie-break
# --------------------------------------------------------------------------


def test_when_both_new_checks_bind_the_zero_cap_correlation_wins():
    risk = _risk(
        annualized_volatility={"H": Decimal("0.20"), "X": Decimal("0.20")},
        correlation={MarketRiskInputs._pair_key("H", "X"): Decimal("0.90")},
    )
    decision = decide(
        make_proposal(symbol="X", quantity=Decimal(100)),
        _held_h(),
        make_limits(
            max_portfolio_volatility_pct=Decimal("0.15"),
            max_position_correlation=Decimal("0.80"),
        ),
        risk,
    )
    # Volatility would MODIFY down to some positive quantity; correlation's
    # cap of 0 is smaller, so it binds and the outcome is a REJECT.
    assert decision.action is PortfolioAction.REJECT
    assert decision.binding_constraint is PortfolioConstraint.POSITION_CORRELATION
    assert decision.approved_quantity == Decimal(0)
