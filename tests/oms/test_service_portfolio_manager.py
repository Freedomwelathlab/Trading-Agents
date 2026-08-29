"""The OMS's integration of the trade-path Portfolio Manager (D029).

These are the tests that pin the two structural properties
apps/api/app/oms/service.py's docstring claims: the Portfolio Manager only
ever sees risk-approved proposals, and a quantity it produces is itself
re-gated by the Risk Engine before any broker call.
"""

from datetime import UTC, datetime
from decimal import Decimal

from apps.api.app.execution.broker import Fill, OrderRequest
from apps.api.app.oms.service import OMSStatus, submit_trade
from apps.api.app.portfolio_manager.models import (
    PortfolioAction,
    PortfolioConstraint,
    PortfolioHolding,
    PortfolioLimits,
    PortfolioState,
)
from apps.api.app.risk.models import (
    AccountState,
    BlockReason,
    RecentOrder,
    RiskLimits,
    Side,
    TradeProposal,
)

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


class SpyBrokerAdapter:
    def __init__(self) -> None:
        self.submit_order_calls: list[OrderRequest] = []

    def submit_order(self, order: OrderRequest, *, market_price: Decimal) -> Fill:
        self.submit_order_calls.append(order)
        return Fill(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            fill_price=market_price,
            filled_at=NOW,
        )

    def get_account_state(self, *, marks):
        raise NotImplementedError


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


def make_account(**overrides) -> AccountState:
    defaults = dict(equity=Decimal(100_000), cash=Decimal(100_000), current_exposure=Decimal(0))
    defaults.update(overrides)
    return AccountState(**defaults)


def make_risk_limits(**overrides) -> RiskLimits:
    defaults = dict(
        max_position_pct_of_equity=Decimal("0.10"),
        max_portfolio_exposure_pct_of_equity=Decimal("0.50"),
        max_risk_pct_of_equity_per_trade=Decimal("0.01"),
        require_stop_price=True,
        max_market_data_age_seconds=60,
        duplicate_order_window_seconds=5,
    )
    defaults.update(overrides)
    return RiskLimits(**defaults)


def make_portfolio_limits(**overrides) -> PortfolioLimits:
    defaults = dict(
        max_symbol_pct_of_equity=Decimal("0.25"),
        min_cash_reserve_pct_of_equity=Decimal("0.05"),
        max_open_positions=20,
    )
    defaults.update(overrides)
    return PortfolioLimits(**defaults)


def test_portfolio_manager_is_skipped_entirely_when_no_portfolio_is_supplied():
    """Pre-D029 behaviour is preserved exactly, and the absence of a
    decision is reported as None - never as an approval."""
    broker = SpyBrokerAdapter()

    result = submit_trade(make_proposal(), make_account(), make_risk_limits(), broker, now=NOW)

    assert result.status is OMSStatus.FILLED
    assert result.portfolio_decision is None
    assert len(broker.submit_order_calls) == 1


def test_a_risk_rejected_proposal_never_reaches_the_portfolio_manager():
    """The Portfolio Manager is a second gate, never a way around the
    first. A risk rejection must short-circuit before it is consulted."""
    broker = SpyBrokerAdapter()

    result = submit_trade(
        make_proposal(quantity=Decimal(1_000)),  # 100k notional > 10% of equity
        make_account(),
        make_risk_limits(),
        broker,
        now=NOW,
        portfolio=PortfolioState(cash=Decimal(100_000)),
        portfolio_limits=make_portfolio_limits(),
    )

    assert result.status is OMSStatus.REJECTED
    assert result.risk_decision.approved is False
    assert result.portfolio_decision is None
    assert broker.submit_order_calls == []


def test_a_portfolio_rejection_stops_a_risk_approved_trade():
    broker = SpyBrokerAdapter()
    portfolio = PortfolioState(
        cash=Decimal(100_000),
        holdings=[
            PortfolioHolding(
                symbol=f"SYM{i}", quantity=Decimal(1), market_value=Decimal(0)
            )
            for i in range(3)
        ],
    )

    result = submit_trade(
        make_proposal(),
        make_account(),
        make_risk_limits(),
        broker,
        now=NOW,
        portfolio=portfolio,
        portfolio_limits=make_portfolio_limits(max_open_positions=3),
    )

    assert result.status is OMSStatus.REJECTED
    # The risk engine DID approve - this rejection is the portfolio's.
    assert result.risk_decision.approved is True
    assert result.portfolio_decision is not None
    assert result.portfolio_decision.action is PortfolioAction.REJECT
    assert (
        result.portfolio_decision.binding_constraint is PortfolioConstraint.MAX_OPEN_POSITIONS
    )
    assert broker.submit_order_calls == []


def test_a_modified_quantity_is_what_actually_reaches_the_broker():
    """Equity 100k, per-symbol cap 25% = 25k, 200 shares already held at
    100 = 20k, so a 100-share buy is cut to 50."""
    broker = SpyBrokerAdapter()
    portfolio = PortfolioState(
        cash=Decimal(80_000),
        holdings=[
            PortfolioHolding(symbol="AAPL", quantity=Decimal(200), market_value=Decimal(20_000))
        ],
    )

    result = submit_trade(
        make_proposal(quantity=Decimal(100)),
        make_account(cash=Decimal(80_000), current_exposure=Decimal(20_000)),
        # A 100-share (10k) order is exactly at the 10% per-trade cap, so
        # the risk engine approves it and the portfolio manager is what
        # cuts it down.
        make_risk_limits(max_risk_pct_of_equity_per_trade=Decimal("0.05")),
        broker,
        now=NOW,
        portfolio=portfolio,
        portfolio_limits=make_portfolio_limits(),
    )

    assert result.status is OMSStatus.FILLED
    assert result.portfolio_decision is not None
    assert result.portfolio_decision.action is PortfolioAction.MODIFY
    assert result.effective_quantity == Decimal(50)
    assert len(broker.submit_order_calls) == 1
    assert broker.submit_order_calls[0].quantity == Decimal(50)
    assert result.fill is not None
    assert result.fill.quantity == Decimal(50)


def test_a_modified_quantity_is_re_evaluated_by_the_risk_engine():
    """The load-bearing safety property: a quantity this system produced
    itself is still gated.

    Most risk checks are monotone in quantity, so a smaller order can only
    be safer - except the D024 duplicate-order check, which is not. Here
    the original 100-share proposal is not a duplicate of anything and the
    Risk Engine approves it; the Portfolio Manager then resizes it to 50,
    which IS an exact duplicate of an order that filled moments ago. The
    re-evaluation catches it and nothing reaches the broker. Without the
    second evaluate_trade() call, this resize would have slipped through a
    risk control unchecked.
    """
    broker = SpyBrokerAdapter()
    portfolio = PortfolioState(
        cash=Decimal(80_000),
        holdings=[
            PortfolioHolding(symbol="AAPL", quantity=Decimal(200), market_value=Decimal(20_000))
        ],
    )
    already_filled = RecentOrder(
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal(50),
        estimated_price=Decimal(100),
        submitted_at=NOW,
    )

    result = submit_trade(
        make_proposal(quantity=Decimal(100)),
        make_account(cash=Decimal(80_000), current_exposure=Decimal(20_000)),
        make_risk_limits(max_risk_pct_of_equity_per_trade=Decimal("0.05")),
        broker,
        now=NOW,
        recent_orders=[already_filled],
        portfolio=portfolio,
        portfolio_limits=make_portfolio_limits(),
    )

    assert result.status is OMSStatus.REJECTED
    assert result.risk_decision.approved is False
    assert result.risk_decision.reason is BlockReason.DUPLICATE_ORDER
    # The portfolio decision that produced the rejected quantity is still
    # reported - the audit record must show why 50 was ever attempted.
    assert result.portfolio_decision is not None
    assert result.portfolio_decision.action is PortfolioAction.MODIFY
    assert result.portfolio_decision.approved_quantity == Decimal(50)
    assert broker.submit_order_calls == []
