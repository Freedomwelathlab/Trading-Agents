"""Integration tests against a real Postgres instance (docker-compose or the
CI postgres service) - these are the tests that actually prove the append-
only Order/Fill schema round-trips correctly, which no pure unit test can.
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
from apps.api.app.risk.models import AccountState, RiskLimits, Side, TradeProposal

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


@contextlib.asynccontextmanager
async def db_session():
    gen = get_session()
    session = await anext(gen)
    try:
        yield session
    finally:
        await gen.aclose()


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
    defaults = dict(equity=Decimal(100_000), cash=Decimal(50_000), current_exposure=Decimal(0))
    defaults.update(overrides)
    return AccountState(**defaults)


def make_limits(**overrides) -> RiskLimits:
    defaults = dict(
        max_position_pct_of_equity=Decimal("0.10"),
        max_portfolio_exposure_pct_of_equity=Decimal("0.50"),
        max_risk_pct_of_equity_per_trade=Decimal("0.01"),
        require_stop_price=True,
        max_market_data_age_seconds=60,
    )
    defaults.update(overrides)
    return RiskLimits(**defaults)


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


@pytest.mark.asyncio
async def test_a_filled_trade_persists_an_order_and_a_fill_row():
    async with db_session() as session, paper_broker_row(session) as broker_id:
        broker = PaperBrokerAdapter(starting_cash=Decimal(50_000))
        result = await submit_trade_and_record(
            session, broker_id, make_proposal(), make_account(), make_limits(), broker, now=NOW
        )
        assert result.status.value == "filled"

        orders = (
            await session.execute(select(OrderRow).where(OrderRow.broker_id == broker_id))
        ).scalars().all()
        assert len(orders) == 1
        assert orders[0].status is OrderStatus.FILLED
        assert orders[0].symbol == "AAPL"
        assert orders[0].risk_block_reason is None

        fills = (
            await session.execute(select(FillRow).where(FillRow.order_id == orders[0].id))
        ).scalars().all()
        assert len(fills) == 1
        assert fills[0].fill_price == Decimal(100)


@pytest.mark.asyncio
async def test_a_rejected_trade_persists_an_order_with_a_reason_and_no_fill():
    async with db_session() as session, paper_broker_row(session) as broker_id:
        broker = PaperBrokerAdapter(starting_cash=Decimal(50_000))
        oversized = make_proposal(quantity=Decimal(200))

        result = await submit_trade_and_record(
            session, broker_id, oversized, make_account(), make_limits(), broker, now=NOW
        )
        assert result.status.value == "rejected"

        orders = (
            await session.execute(select(OrderRow).where(OrderRow.broker_id == broker_id))
        ).scalars().all()
        assert len(orders) == 1
        assert orders[0].status is OrderStatus.REJECTED
        assert orders[0].risk_block_reason == "exceeds_max_position_size"

        fills = (
            await session.execute(select(FillRow).where(FillRow.order_id == orders[0].id))
        ).scalars().all()
        assert fills == []


@pytest.mark.asyncio
async def test_each_submission_is_a_new_row_never_an_update():
    async with db_session() as session, paper_broker_row(session) as broker_id:
        broker = PaperBrokerAdapter(starting_cash=Decimal(50_000))
        await submit_trade_and_record(
            session, broker_id, make_proposal(), make_account(), make_limits(), broker, now=NOW
        )
        await submit_trade_and_record(
            session,
            broker_id,
            make_proposal(quantity=Decimal(1)),
            make_account(),
            make_limits(),
            broker,
            now=NOW,
        )

        orders = (
            await session.execute(select(OrderRow).where(OrderRow.broker_id == broker_id))
        ).scalars().all()
        assert len(orders) == 2
