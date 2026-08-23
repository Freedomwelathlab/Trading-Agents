"""Integration tests against a real Postgres instance - load_paper_broker/
save_paper_broker exist specifically to round-trip through the database,
so there's no meaningful pure-unit version of these tests.
"""

import contextlib
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from apps.api.app.db.base import get_session
from apps.api.app.db.models import Broker, BrokerAccount, BrokerKind, BrokerPosition
from apps.api.app.execution.broker import OrderRequest
from apps.api.app.execution.persistence import load_paper_broker, save_paper_broker
from apps.api.app.risk.models import Side


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
        Broker(id=broker_id, name="Test Broker", kind=BrokerKind.PAPER, provider="paper-sim")
    )
    await session.commit()
    try:
        yield broker_id
    finally:
        await session.execute(delete(BrokerPosition).where(BrokerPosition.broker_id == broker_id))
        await session.execute(delete(BrokerAccount).where(BrokerAccount.broker_id == broker_id))
        await session.execute(delete(Broker).where(Broker.id == broker_id))
        await session.commit()


@pytest.mark.asyncio
async def test_loading_a_broker_with_no_existing_row_seeds_the_default_starting_cash():
    async with db_session() as session, paper_broker_row(session) as broker_id:
        broker = await load_paper_broker(session, broker_id, default_starting_cash=Decimal(50_000))
        await session.commit()

        assert broker.cash == Decimal(50_000)
        assert broker.positions == {}

        account = (
            await session.execute(select(BrokerAccount).where(BrokerAccount.broker_id == broker_id))
        ).scalar_one()
        assert account.cash == Decimal(50_000)


@pytest.mark.asyncio
async def test_state_survives_a_load_save_load_round_trip():
    async with db_session() as session, paper_broker_row(session) as broker_id:
        broker = await load_paper_broker(session, broker_id, default_starting_cash=Decimal(100_000))
        broker.submit_order(
            OrderRequest(symbol="AAPL", side=Side.BUY, quantity=Decimal(10)),
            market_price=Decimal(100),
        )
        await save_paper_broker(session, broker_id, broker)
        await session.commit()

        reloaded = await load_paper_broker(
            session, broker_id, default_starting_cash=Decimal(100_000)
        )
        assert reloaded.cash == Decimal(99_000)
        assert reloaded.positions == {"AAPL": Decimal(10)}


@pytest.mark.asyncio
async def test_closing_a_position_back_to_zero_removes_its_row_not_a_zero_row():
    async with db_session() as session, paper_broker_row(session) as broker_id:
        broker = await load_paper_broker(session, broker_id, default_starting_cash=Decimal(100_000))
        broker.submit_order(
            OrderRequest(symbol="AAPL", side=Side.BUY, quantity=Decimal(10)),
            market_price=Decimal(100),
        )
        broker.submit_order(
            OrderRequest(symbol="AAPL", side=Side.SELL, quantity=Decimal(10)),
            market_price=Decimal(110),
        )
        await save_paper_broker(session, broker_id, broker)
        await session.commit()

        rows = (
            await session.execute(
                select(BrokerPosition).where(BrokerPosition.broker_id == broker_id)
            )
        ).scalars().all()
        assert rows == []

        reloaded = await load_paper_broker(
            session, broker_id, default_starting_cash=Decimal(100_000)
        )
        assert reloaded.positions == {}
        assert reloaded.cash == Decimal(100_000) + Decimal(100)  # bought at 100, sold at 110
