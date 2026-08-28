from datetime import UTC, datetime
from decimal import Decimal

from apps.api.app.execution.broker import Fill, OrderRequest
from apps.api.app.execution.paper_broker import PaperBrokerAdapter
from apps.api.app.oms.service import OMSStatus, submit_trade
from apps.api.app.risk.models import AccountState, RecentOrder, RiskLimits, Side, TradeProposal

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


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


def make_limits(**overrides) -> RiskLimits:
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


class SpyBrokerAdapter:
    """Records whether submit_order was ever called, so tests can prove a
    rejected trade never reaches the broker."""

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


def make_account(**overrides) -> AccountState:
    defaults = dict(equity=Decimal(100_000), cash=Decimal(50_000), current_exposure=Decimal(0))
    defaults.update(overrides)
    return AccountState(**defaults)


def test_approved_trade_reaches_the_broker_and_fills():
    broker = PaperBrokerAdapter(starting_cash=Decimal(50_000))
    result = submit_trade(make_proposal(), make_account(), make_limits(), broker, now=NOW)

    assert result.status is OMSStatus.FILLED
    assert result.risk_decision.approved is True
    assert result.fill is not None
    assert result.fill.fill_price == Decimal(100)
    assert len(broker.fills) == 1


def test_rejected_trade_never_reaches_the_broker():
    spy = SpyBrokerAdapter()
    # quantity 200 * price 100 = 20,000 notional, exceeds 10% of equity (10,000)
    oversized = make_proposal(quantity=Decimal(200))

    result = submit_trade(oversized, make_account(), make_limits(), spy, now=NOW)

    assert result.status is OMSStatus.REJECTED
    assert result.risk_decision.approved is False
    assert result.fill is None
    assert spy.submit_order_calls == []


def test_emergency_stop_blocks_before_the_broker_is_touched():
    spy = SpyBrokerAdapter()

    result = submit_trade(
        make_proposal(), make_account(), make_limits(), spy, emergency_stop_active=True, now=NOW
    )

    assert result.status is OMSStatus.REJECTED
    assert spy.submit_order_calls == []


def test_recent_orders_passed_through_to_the_engine_block_a_duplicate_before_the_broker():
    spy = SpyBrokerAdapter()
    recent = [
        RecentOrder(
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal(10),
            estimated_price=Decimal(100),
            submitted_at=NOW,
        )
    ]

    result = submit_trade(
        make_proposal(), make_account(), make_limits(), spy, now=NOW, recent_orders=recent
    )

    assert result.status is OMSStatus.REJECTED
    assert result.risk_decision.reason.value == "duplicate_order"
    assert spy.submit_order_calls == []
