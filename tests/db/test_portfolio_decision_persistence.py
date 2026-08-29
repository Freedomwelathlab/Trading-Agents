"""Integration tests against a real Postgres instance: the orders.portfolio_*
audit columns migration 0009 added (D029). Spec §18 requires the Portfolio
Manager to produce a complete audit record; only a real round-trip through
the real schema proves that record actually survives.
"""

import contextlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from apps.api.app.db.base import get_session
from apps.api.app.db.models import Broker, BrokerKind, OrderStatus
from apps.api.app.db.models import Fill as FillRow
from apps.api.app.db.models import Order as OrderRow
from apps.api.app.execution.paper_broker import PaperBrokerAdapter
from apps.api.app.oms.persistence import submit_trade_and_record
from apps.api.app.portfolio_manager.models import (
    PortfolioHolding,
    PortfolioLimits,
    PortfolioState,
)
from apps.api.app.risk.models import AccountState, RiskLimits, Side, TradeProposal

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


@contextlib.asynccontextmanager
async def db_session():
    gen = get_session()
    session = await anext(gen)
    try:
        yield session
    finally:
        await gen.aclose()


@contextlib.asynccontextmanager
async def paper_broker_row(session):
    broker_id = uuid.uuid4()
    session.add(
        Broker(id=broker_id, name="Test Paper Broker", kind=BrokerKind.PAPER, provider="paper-sim")
    )
    await session.commit()
    try:
        yield broker_id
    finally:
        await session.execute(
            delete(FillRow).where(
                FillRow.order_id.in_(select(OrderRow.id).where(OrderRow.broker_id == broker_id))
            )
        )
        await session.execute(delete(OrderRow).where(OrderRow.broker_id == broker_id))
        await session.execute(delete(Broker).where(Broker.id == broker_id))
        await session.commit()


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


def make_risk_limits(**overrides) -> RiskLimits:
    defaults = dict(
        max_position_pct_of_equity=Decimal("0.10"),
        max_portfolio_exposure_pct_of_equity=Decimal("0.50"),
        max_risk_pct_of_equity_per_trade=Decimal("0.05"),
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


async def _only_order(session, broker_id) -> OrderRow:
    orders = (
        await session.execute(select(OrderRow).where(OrderRow.broker_id == broker_id))
    ).scalars().all()
    assert len(orders) == 1
    return orders[0]


@pytest.mark.asyncio
async def test_an_approved_trade_persists_an_approve_audit_record():
    async with db_session() as session, paper_broker_row(session) as broker_id:
        broker = PaperBrokerAdapter(starting_cash=Decimal(100_000))

        result = await submit_trade_and_record(
            session,
            broker_id,
            make_proposal(),
            AccountState(
                equity=Decimal(100_000), cash=Decimal(100_000), current_exposure=Decimal(0)
            ),
            make_risk_limits(),
            broker,
            now=NOW,
            portfolio=PortfolioState(cash=Decimal(100_000)),
            portfolio_limits=make_portfolio_limits(),
        )
        assert result.status.value == "filled"

        order = await _only_order(session, broker_id)
        assert order.status is OrderStatus.FILLED
        assert order.portfolio_action == "approve"
        assert order.portfolio_binding_constraint is None
        assert order.portfolio_requested_quantity == Decimal(10)
        assert order.quantity == Decimal(10)


@pytest.mark.asyncio
async def test_a_modified_trade_persists_both_quantities():
    """The resize must be visible in the audit trail: `quantity` is what was
    acted on, `portfolio_requested_quantity` what was asked for."""
    async with db_session() as session, paper_broker_row(session) as broker_id:
        broker = PaperBrokerAdapter(
            starting_cash=Decimal(80_000), positions={"AAPL": Decimal(200)}
        )
        portfolio = PortfolioState(
            cash=Decimal(80_000),
            holdings=[
                PortfolioHolding(
                    symbol="AAPL", quantity=Decimal(200), market_value=Decimal(20_000)
                )
            ],
        )

        result = await submit_trade_and_record(
            session,
            broker_id,
            make_proposal(quantity=Decimal(100)),
            AccountState(
                equity=Decimal(100_000),
                cash=Decimal(80_000),
                current_exposure=Decimal(20_000),
            ),
            make_risk_limits(),
            broker,
            now=NOW,
            portfolio=portfolio,
            portfolio_limits=make_portfolio_limits(),
        )
        assert result.status.value == "filled"

        order = await _only_order(session, broker_id)
        assert order.portfolio_action == "modify"
        assert order.portfolio_binding_constraint == "symbol_concentration"
        assert order.portfolio_requested_quantity == Decimal(100)
        assert order.quantity == Decimal(50)

        fills = (
            await session.execute(select(FillRow).where(FillRow.order_id == order.id))
        ).scalars().all()
        assert len(fills) == 1
        assert fills[0].quantity == Decimal(50)
        # And the broker really moved only 50 shares' worth of cash.
        assert broker.positions["AAPL"] == Decimal(250)
        assert broker.cash == Decimal(75_000)


@pytest.mark.asyncio
async def test_a_portfolio_rejection_persists_a_rejected_order_with_no_risk_block_reason():
    """The audit row must make it unambiguous that the Risk Engine passed
    this trade and the Portfolio Manager stopped it."""
    async with db_session() as session, paper_broker_row(session) as broker_id:
        broker = PaperBrokerAdapter(starting_cash=Decimal(100_000))
        portfolio = PortfolioState(
            cash=Decimal(100_000),
            holdings=[
                PortfolioHolding(
                    symbol=f"SYM{i}", quantity=Decimal(1), market_value=Decimal(0)
                )
                for i in range(2)
            ],
        )

        result = await submit_trade_and_record(
            session,
            broker_id,
            make_proposal(),
            AccountState(
                equity=Decimal(100_000), cash=Decimal(100_000), current_exposure=Decimal(0)
            ),
            make_risk_limits(),
            broker,
            now=NOW,
            portfolio=portfolio,
            portfolio_limits=make_portfolio_limits(max_open_positions=2),
        )
        assert result.status.value == "rejected"

        order = await _only_order(session, broker_id)
        assert order.status is OrderStatus.REJECTED
        assert order.risk_block_reason is None  # the risk engine approved it
        assert order.portfolio_action == "reject"
        assert order.portfolio_binding_constraint == "max_open_positions"
        assert order.portfolio_requested_quantity == Decimal(10)

        fills = (
            await session.execute(select(FillRow).where(FillRow.order_id == order.id))
        ).scalars().all()
        assert fills == []
        # Nothing moved at the broker either.
        assert broker.cash == Decimal(100_000)
        assert broker.positions == {}


@pytest.mark.asyncio
async def test_a_risk_rejected_order_records_no_portfolio_decision_at_all():
    """Null portfolio_action means 'never ran', never 'approved'."""
    async with db_session() as session, paper_broker_row(session) as broker_id:
        broker = PaperBrokerAdapter(starting_cash=Decimal(100_000))

        result = await submit_trade_and_record(
            session,
            broker_id,
            make_proposal(quantity=Decimal(1_000)),
            AccountState(
                equity=Decimal(100_000), cash=Decimal(100_000), current_exposure=Decimal(0)
            ),
            make_risk_limits(),
            broker,
            now=NOW,
            portfolio=PortfolioState(cash=Decimal(100_000)),
            portfolio_limits=make_portfolio_limits(),
        )
        assert result.status.value == "rejected"

        order = await _only_order(session, broker_id)
        assert order.risk_block_reason == "exceeds_max_position_size"
        assert order.portfolio_action is None
        assert order.portfolio_binding_constraint is None
        assert order.portfolio_requested_quantity is None
