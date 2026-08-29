"""Pure unit tests for the trade-path Portfolio Manager (D029). No DB, no
network - the component itself has no I/O, so neither do these."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.portfolio_manager.manager import decide, portfolio_state_from_positions
from apps.api.app.portfolio_manager.models import (
    PortfolioAction,
    PortfolioConstraint,
    PortfolioHolding,
    PortfolioLimits,
    PortfolioState,
)
from apps.api.app.risk.models import Side, TradeProposal

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


def make_proposal(**overrides) -> TradeProposal:
    defaults = dict(
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal(10),
        estimated_price=Decimal(100),
        stop_price=Decimal(95),
        market_data_as_of=NOW,
    )
    defaults.update(overrides)
    return TradeProposal(**defaults)


def make_limits(**overrides) -> PortfolioLimits:
    defaults = dict(
        max_symbol_pct_of_equity=Decimal("0.25"),
        min_cash_reserve_pct_of_equity=Decimal("0.05"),
        max_open_positions=20,
    )
    defaults.update(overrides)
    return PortfolioLimits(**defaults)


def test_approves_a_trade_that_breaches_nothing():
    portfolio = PortfolioState(cash=Decimal(100_000), holdings=[])

    decision = decide(make_proposal(), portfolio, make_limits())

    assert decision.action is PortfolioAction.APPROVE
    assert decision.approved_quantity == Decimal(10)
    assert decision.requested_quantity == Decimal(10)
    assert decision.binding_constraint is None
    # The audit record lists every constraint considered, not only failures.
    assert {c.constraint for c in decision.checks} == set(PortfolioConstraint)
    assert all(c.passed for c in decision.checks)


def test_modifies_a_buy_that_would_over_concentrate_one_symbol():
    """The gap this component exists to close: an existing position plus a
    new order, each individually fine for the Risk Engine, together exceed
    the aggregate per-symbol cap. Equity 100k, cap 25% = 25k; 200 shares
    already held at 100 = 20k, so only 50 more shares fit."""
    portfolio = PortfolioState(
        cash=Decimal(80_000),
        holdings=[
            PortfolioHolding(
                symbol="AAPL", quantity=Decimal(200), market_value=Decimal(20_000)
            )
        ],
    )

    decision = decide(make_proposal(quantity=Decimal(100)), portfolio, make_limits())

    assert decision.action is PortfolioAction.MODIFY
    assert decision.binding_constraint is PortfolioConstraint.SYMBOL_CONCENTRATION
    assert decision.approved_quantity == Decimal(50)
    assert decision.requested_quantity == Decimal(100)
    concentration = next(
        c for c in decision.checks if c.constraint is PortfolioConstraint.SYMBOL_CONCENTRATION
    )
    assert concentration.passed is False
    assert concentration.worsened_by_trade is True
    assert concentration.projected_value == Decimal(30_000)
    assert concentration.limit_value == Decimal(25_000)


def test_modifies_a_buy_that_would_breach_the_cash_reserve():
    """Equity 100k (all cash), 5% reserve = 5k floor, so at most 95k of the
    100k cash may be spent: 950 shares at 100, not the 980 requested."""
    portfolio = PortfolioState(cash=Decimal(100_000), holdings=[])

    decision = decide(
        make_proposal(quantity=Decimal(980)),
        portfolio,
        make_limits(max_symbol_pct_of_equity=Decimal(1)),
    )

    assert decision.action is PortfolioAction.MODIFY
    assert decision.binding_constraint is PortfolioConstraint.CASH_RESERVE
    assert decision.approved_quantity == Decimal(950)


def test_rejects_a_new_symbol_once_the_open_position_cap_is_reached():
    """MAX_OPEN_POSITIONS has no partial satisfaction: any quantity >= 1
    opens exactly one more symbol, so the only compliant size is zero."""
    portfolio = PortfolioState(
        cash=Decimal(90_000),
        holdings=[
            PortfolioHolding(
                symbol=f"SYM{i}", quantity=Decimal(10), market_value=Decimal(1_000)
            )
            for i in range(10)
        ],
    )

    decision = decide(
        make_proposal(quantity=Decimal(5)), portfolio, make_limits(max_open_positions=10)
    )

    assert decision.action is PortfolioAction.REJECT
    assert decision.binding_constraint is PortfolioConstraint.MAX_OPEN_POSITIONS
    assert decision.approved_quantity == Decimal(0)


def test_adding_to_an_existing_symbol_is_not_blocked_by_the_open_position_cap():
    """Buying more of something already held does not open a new position,
    so the count is not worsened and must not bind - even at the cap."""
    portfolio = PortfolioState(
        cash=Decimal(90_000),
        holdings=[
            PortfolioHolding(
                symbol=f"SYM{i}", quantity=Decimal(10), market_value=Decimal(1_000)
            )
            for i in range(10)
        ],
    )

    decision = decide(
        make_proposal(symbol="SYM3", quantity=Decimal(5)),
        portfolio,
        make_limits(max_open_positions=10),
    )

    assert decision.action is PortfolioAction.APPROVE
    count_check = next(
        c for c in decision.checks if c.constraint is PortfolioConstraint.MAX_OPEN_POSITIONS
    )
    assert count_check.worsened_by_trade is False


def test_a_de_risking_sell_is_never_blocked_by_an_already_breached_limit():
    """A portfolio that is ALREADY over-concentrated must not have the sell
    that relieves it refused because of the very breach it relieves. The
    check still records the failure honestly; it simply isn't binding."""
    portfolio = PortfolioState(
        cash=Decimal(1_000),
        holdings=[
            PortfolioHolding(
                symbol="AAPL", quantity=Decimal(900), market_value=Decimal(90_000)
            )
        ],
    )

    decision = decide(
        make_proposal(side=Side.SELL, quantity=Decimal(100)), portfolio, make_limits()
    )

    assert decision.action is PortfolioAction.APPROVE
    concentration = next(
        c for c in decision.checks if c.constraint is PortfolioConstraint.SYMBOL_CONCENTRATION
    )
    assert concentration.passed is False  # 80k still over the 22.75k cap
    assert concentration.worsened_by_trade is False  # but the sell improved it


def test_rejects_a_buy_that_cannot_be_shrunk_to_even_one_share():
    """A single share at 40k already breaches the 25k cap, so there is no
    compliant size at all - REJECT, never a zero-quantity 'approval'."""
    portfolio = PortfolioState(cash=Decimal(100_000), holdings=[])

    decision = decide(
        make_proposal(quantity=Decimal(2), estimated_price=Decimal(40_000)),
        portfolio,
        make_limits(),
    )

    assert decision.action is PortfolioAction.REJECT
    assert decision.approved_quantity == Decimal(0)
    assert decision.binding_constraint is PortfolioConstraint.SYMBOL_CONCENTRATION


def test_the_tightest_of_several_breached_constraints_binds():
    """Equity 100k. Concentration (2.5% = 2.5k) allows 25 shares at 100; the
    cash reserve (5% = 5k floor against 6.5k cash) allows only 15. The
    smaller cap must win, and be the one named in the audit record."""
    portfolio = PortfolioState(
        cash=Decimal(6_500),
        holdings=[
            PortfolioHolding(
                symbol="MSFT", quantity=Decimal(935), market_value=Decimal(93_500)
            )
        ],
    )

    decision = decide(
        make_proposal(quantity=Decimal(60)),
        portfolio,
        make_limits(max_symbol_pct_of_equity=Decimal("0.025"), max_open_positions=20),
    )

    assert decision.action is PortfolioAction.MODIFY
    assert decision.binding_constraint is PortfolioConstraint.CASH_RESERVE
    assert decision.approved_quantity == Decimal(15)


def test_rejects_fail_closed_on_non_positive_equity():
    """Every limit is a fraction of equity; zero equity makes them all
    meaningless rather than generous (docs/TRADING_SAFETY.md)."""
    portfolio = PortfolioState(cash=Decimal(0), holdings=[])

    decision = decide(make_proposal(), portfolio, make_limits())

    assert decision.action is PortfolioAction.REJECT
    assert decision.binding_constraint is None
    assert decision.checks == []


def test_never_emits_request_more_research():
    """Spec §18 lists it; D029's deterministic implementation never returns
    it. This test is the guard on that documented promise."""
    portfolio = PortfolioState(cash=Decimal(100_000), holdings=[])
    for quantity in (Decimal(1), Decimal(500), Decimal(10_000)):
        decision = decide(make_proposal(quantity=quantity), portfolio, make_limits())
        assert decision.action is not PortfolioAction.REQUEST_MORE_RESEARCH


def test_decide_is_pure_and_leaves_the_portfolio_untouched():
    portfolio = PortfolioState(
        cash=Decimal(100_000),
        holdings=[
            PortfolioHolding(symbol="AAPL", quantity=Decimal(10), market_value=Decimal(1_000))
        ],
    )
    before = portfolio.model_dump()

    decide(make_proposal(quantity=Decimal(400)), portfolio, make_limits())

    assert portfolio.model_dump() == before


def test_portfolio_state_from_positions_refuses_to_value_an_unmarked_position():
    with pytest.raises(ValueError, match="No mark supplied for open position AAPL"):
        portfolio_state_from_positions(
            {"AAPL": Decimal(10)}, {"MSFT": Decimal(50)}, Decimal(1_000)
        )


def test_portfolio_state_from_positions_skips_flat_symbols():
    state = portfolio_state_from_positions(
        {"AAPL": Decimal(10), "MSFT": Decimal(0)},
        {"AAPL": Decimal(100)},
        Decimal(1_000),
    )

    assert state.open_symbols == {"AAPL"}
    assert state.equity == Decimal(2_000)
    assert state.held_value("AAPL") == Decimal(1_000)
