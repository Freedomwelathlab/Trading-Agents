"""Integration tests for the trade-path Portfolio Manager over real HTTP,
against a real Postgres instance (D029).

Same harness rationale as tests/api/test_trades.py (httpx.AsyncClient +
ASGITransport rather than TestClient - see D007/D009). These tests exist
because the unit tests in tests/portfolio_manager/ prove the decision logic
and tests/oms/ proves the wiring, but only this level proves that a real
request against real persisted broker state produces a real resize.
"""

import contextlib
import uuid
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from apps.api.app.auth.permissions import Permission
from apps.api.app.auth.security import hash_password
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    Broker,
    BrokerAccount,
    BrokerGrant,
    BrokerKind,
    BrokerPosition,
    Role,
    User,
)
from apps.api.app.db.models import Fill as FillRow
from apps.api.app.db.models import Order as OrderRow
from apps.api.app.main import app

TEST_PASSWORD = "correct-horse-battery-staple"


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
    session.add(Broker(id=broker_id, name="Test Broker", kind=BrokerKind.PAPER, provider="paper"))
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
        await session.execute(delete(BrokerGrant).where(BrokerGrant.broker_id == broker_id))
        await session.execute(delete(BrokerPosition).where(BrokerPosition.broker_id == broker_id))
        await session.execute(delete(BrokerAccount).where(BrokerAccount.broker_id == broker_id))
        await session.execute(delete(Broker).where(Broker.id == broker_id))
        await session.commit()


@contextlib.asynccontextmanager
async def trading_user(session):
    """Entered BEFORE paper_broker_row, so it exits after it: `orders` rows
    reference both the broker and the submitting user, and paper_broker_row
    is the one that deletes them."""
    user_id = uuid.uuid4()
    role_id = uuid.uuid4()
    email = f"{uuid.uuid4()}@example.com"
    session.add(
        Role(
            id=role_id,
            name=f"test-role-{role_id}",
            permissions=[Permission.SUBMIT_PAPER_TRADE.value],
        )
    )
    session.add(
        User(
            id=user_id,
            email=email,
            hashed_password=hash_password(TEST_PASSWORD),
            is_active=True,
            role_id=role_id,
        )
    )
    await session.commit()
    try:
        yield user_id, email
    finally:
        await session.execute(delete(BrokerGrant).where(BrokerGrant.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.execute(delete(Role).where(Role.id == role_id))
        await session.commit()


@contextlib.asynccontextmanager
async def broker_grant(session, *, user_id, broker_id):
    session.add(BrokerGrant(user_id=user_id, broker_id=broker_id))
    await session.commit()
    try:
        yield
    finally:
        await session.execute(
            delete(BrokerGrant).where(
                BrokerGrant.user_id == user_id, BrokerGrant.broker_id == broker_id
            )
        )
        await session.commit()


@contextlib.asynccontextmanager
async def api_client():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@contextlib.contextmanager
def settings_override(**overrides):
    """Portfolio limits come from Settings, so exercising a limit that the
    default config makes unreachable (20 open positions) means overriding
    the dependency, not fabricating a different code path."""
    base = get_settings()
    patched = Settings(**{**base.model_dump(), **overrides})
    app.dependency_overrides[get_settings] = lambda: patched
    try:
        yield
    finally:
        del app.dependency_overrides[get_settings]


async def _token(client: AsyncClient, email: str) -> str:
    response = await client.post(
        "/auth/login", data={"username": email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


async def _trade(client, broker_id, token, **body):
    return await client.post(
        f"/brokers/{broker_id}/trades",
        headers={"Authorization": f"Bearer {token}"},
        json={"symbol": "AAPL", "side": "buy", "stop_price": "95", **body},
    )


@pytest.mark.asyncio
async def test_an_uncontested_trade_reports_an_approve_decision_over_http():
    async with (
        db_session() as session,
        trading_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _token(client, email)
            response = await _trade(
                client, broker_id, token, quantity="10", estimated_price="100"
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "filled"
        assert body["approved"] is True
        assert body["portfolio_action"] == "approve"
        assert body["portfolio_binding_constraint"] is None
        assert body["portfolio_requested_quantity"] == "10"
        assert body["fill_quantity"] == "10"


@pytest.mark.asyncio
async def test_accumulated_position_is_cut_down_by_the_portfolio_manager():
    """Three separate buys, each one individually acceptable to the Risk
    Engine (10% of equity per order), together build a position the 25%
    aggregate cap will not allow. 100k equity, 199 shares at 100 already
    held (19,900), cap 25,000 -> only 51 more shares fit, so the third
    98-share order is resized to 51.

    This is exactly the gap D029 exists to close: no per-trade check ever
    sees the accumulated position.
    """
    async with (
        db_session() as session,
        trading_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _token(client, email)

            first = await _trade(client, broker_id, token, quantity="100", estimated_price="100")
            assert first.status_code == 200, first.text
            assert first.json()["fill_quantity"] == "100"
            assert first.json()["portfolio_action"] == "approve"

            # 99, not another 100 - an identical repeat inside the D024
            # duplicate window would be blocked by the risk engine before
            # the portfolio manager is ever reached.
            second = await _trade(client, broker_id, token, quantity="99", estimated_price="100")
            assert second.status_code == 200, second.text
            assert second.json()["fill_quantity"] == "99"

            # 98 for the same reason: distinct from both prior orders, so
            # the duplicate-order check is not what stops it.
            third = await _trade(client, broker_id, token, quantity="98", estimated_price="100")

        assert third.status_code == 200, third.text
        body = third.json()
        assert body["approved"] is True  # the risk engine passed it
        assert body["portfolio_action"] == "modify"
        assert body["portfolio_binding_constraint"] == "symbol_concentration"
        assert body["portfolio_requested_quantity"] == "98"
        assert body["status"] == "filled"
        assert body["fill_quantity"] == "51"

        # The broker's persisted state reflects the resized trade, not the
        # requested one: 199 + 51 = 250 shares, 100000 - 25000 = 75000 cash.
        position = (
            await session.execute(
                select(BrokerPosition).where(BrokerPosition.broker_id == broker_id)
            )
        ).scalars().one()
        assert position.quantity == Decimal(250)
        account = (
            await session.execute(
                select(BrokerAccount).where(BrokerAccount.broker_id == broker_id)
            )
        ).scalars().one()
        assert account.cash == Decimal(75_000)

        # And the audit row records both quantities.
        order = (
            await session.execute(
                select(OrderRow)
                .where(OrderRow.broker_id == broker_id)
                .order_by(OrderRow.submitted_at.desc())
                .limit(1)
            )
        ).scalars().one()
        assert order.quantity == Decimal(51)
        assert order.portfolio_requested_quantity == Decimal(98)
        assert order.portfolio_action == "modify"


@pytest.mark.asyncio
async def test_the_open_position_cap_rejects_a_new_symbol_over_http():
    async with (
        db_session() as session,
        trading_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        with settings_override(portfolio_max_open_positions=1):
            async with api_client() as client:
                token = await _token(client, email)

                first = await _trade(
                    client, broker_id, token, quantity="10", estimated_price="100"
                )
                assert first.status_code == 200, first.text
                assert first.json()["status"] == "filled"

                second = await _trade(
                    client,
                    broker_id,
                    token,
                    symbol="MSFT",
                    quantity="10",
                    estimated_price="100",
                    marks={"AAPL": "100"},
                )

        assert second.status_code == 200, second.text
        body = second.json()
        assert body["status"] == "rejected"
        assert body["approved"] is True  # risk passed; the portfolio didn't
        assert body["block_reason"] is None
        assert body["portfolio_action"] == "reject"
        assert body["portfolio_binding_constraint"] == "max_open_positions"
        assert body["fill_quantity"] is None

        # Nothing was bought: still one position, and cash unchanged from
        # the first trade.
        positions = (
            await session.execute(
                select(BrokerPosition).where(BrokerPosition.broker_id == broker_id)
            )
        ).scalars().all()
        assert {p.symbol for p in positions} == {"AAPL"}
        account = (
            await session.execute(
                select(BrokerAccount).where(BrokerAccount.broker_id == broker_id)
            )
        ).scalars().one()
        assert account.cash == Decimal(99_000)


@pytest.mark.asyncio
async def test_a_risk_rejection_reports_no_portfolio_decision_over_http():
    async with (
        db_session() as session,
        trading_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _token(client, email)
            response = await _trade(
                client, broker_id, token, quantity="1000", estimated_price="100"
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "rejected"
        assert body["approved"] is False
        assert body["block_reason"] == "exceeds_max_position_size"
        assert body["portfolio_action"] is None
        assert body["portfolio_requested_quantity"] is None
