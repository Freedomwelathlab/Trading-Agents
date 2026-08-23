"""Integration tests against a real Postgres instance - the endpoint wires
together the DB, auth, the paper broker registry, and the risk engine, so
this is the level that actually proves the whole path works.

Uses httpx.AsyncClient + ASGITransport rather than FastAPI's TestClient:
TestClient runs the app in a separate thread with its own event loop, which
is incompatible with the module-level, loop-bound DB engine
(apps/api/app/db/base.py) once a test also touches that engine directly -
see docs/DECISIONS.md D007/D009. AsyncClient runs in-process on the same
event loop as the rest of the test, avoiding the mismatch entirely.
"""

import contextlib
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from apps.api.app.auth.security import hash_password
from apps.api.app.db.base import get_session
from apps.api.app.db.models import Broker, BrokerKind, User
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
async def paper_broker_row(session, *, kind: BrokerKind = BrokerKind.PAPER):
    broker_id = uuid.uuid4()
    session.add(Broker(id=broker_id, name="Test Broker", kind=kind, provider="paper-sim"))
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


@contextlib.asynccontextmanager
async def active_user(session, *, is_active: bool = True):
    user_id = uuid.uuid4()
    email = f"{uuid.uuid4()}@example.com"
    session.add(
        User(
            id=user_id,
            email=email,
            hashed_password=hash_password(TEST_PASSWORD),
            is_active=is_active,
        )
    )
    await session.commit()
    try:
        yield user_id, email
    finally:
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()


@contextlib.asynccontextmanager
async def api_client():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def _get_token(client: AsyncClient, email: str) -> str:
    response = await client.post(
        "/auth/login", data={"username": email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_a_well_formed_trade_is_approved_and_filled():
    async with (
        db_session() as session,
        active_user(session) as (_user_id, email),
        paper_broker_row(session) as broker_id,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                f"/brokers/{broker_id}/trades",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "filled"
        assert body["approved"] is True
        assert body["fill_price"] == "100"
        assert body["fill_quantity"] == "10"
        assert body["order_id"] is not None


@pytest.mark.asyncio
async def test_a_submitted_order_records_who_submitted_it():
    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                f"/brokers/{broker_id}/trades",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )
        order_id = uuid.UUID(response.json()["order_id"])
        order = (
            await session.execute(select(OrderRow).where(OrderRow.id == order_id))
        ).scalar_one()
        assert order.submitted_by_user_id == user_id


@pytest.mark.asyncio
async def test_an_oversized_trade_is_rejected_with_a_reason_and_no_fill():
    async with (
        db_session() as session,
        active_user(session) as (_user_id, email),
        paper_broker_row(session) as broker_id,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                f"/brokers/{broker_id}/trades",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "20000",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "rejected"
        assert body["approved"] is False
        assert body["block_reason"] == "exceeds_max_position_size"
        assert body["fill_price"] is None


@pytest.mark.asyncio
async def test_a_request_with_no_token_is_rejected():
    async with db_session() as session, paper_broker_row(session) as broker_id:
        async with api_client() as client:
            response = await client.post(
                f"/brokers/{broker_id}/trades",
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_request_with_an_invalid_token_is_rejected():
    async with db_session() as session, paper_broker_row(session) as broker_id:
        async with api_client() as client:
            response = await client.post(
                f"/brokers/{broker_id}/trades",
                headers={"Authorization": "Bearer not-a-real-token"},
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_an_inactive_user_cannot_trade_even_with_a_previously_valid_token():
    async with (
        db_session() as session,
        active_user(session) as (_user_id, email),
        paper_broker_row(session) as broker_id,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            user = (await session.execute(select(User).where(User.email == email))).scalar_one()
            user.is_active = False
            await session.commit()

            response = await client.post(
                f"/brokers/{broker_id}/trades",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_unknown_broker_returns_404():
    async with db_session() as session, active_user(session) as (_user_id, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                f"/brokers/{uuid.uuid4()}/trades",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_live_broker_is_rejected_since_no_live_execution_path_exists():
    async with (
        db_session() as session,
        active_user(session) as (_user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                f"/brokers/{broker_id}/trades",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )
        assert response.status_code == 400
        assert "NOT_CONFIGURED" in response.json()["detail"]


@pytest.mark.asyncio
async def test_emergency_stop_blocks_the_trade():
    from apps.api.app.core.config import get_settings

    async with (
        db_session() as session,
        active_user(session) as (_user_id, email),
        paper_broker_row(session) as broker_id,
    ):
        settings = get_settings()
        original = settings.emergency_stop_active
        settings.emergency_stop_active = True
        try:
            async with api_client() as client:
                token = await _get_token(client, email)
                response = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "AAPL",
                        "side": "buy",
                        "quantity": "10",
                        "estimated_price": "100",
                        "stop_price": "95",
                    },
                )
        finally:
            settings.emergency_stop_active = original

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "rejected"
        assert body["block_reason"] == "emergency_stop_active"


@pytest.mark.asyncio
async def test_missing_mark_for_an_existing_position_is_a_400_not_a_guess():
    async with (
        db_session() as session,
        active_user(session) as (_user_id, email),
        paper_broker_row(session) as broker_id,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            # First trade establishes an AAPL position.
            await client.post(
                f"/brokers/{broker_id}/trades",
                headers=headers,
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )
            # Second trade on a different symbol omits a mark for the held AAPL position.
            response = await client.post(
                f"/brokers/{broker_id}/trades",
                headers=headers,
                json={
                    "symbol": "MSFT",
                    "side": "buy",
                    "quantity": "1",
                    "estimated_price": "50",
                    "stop_price": "45",
                },
            )

        assert response.status_code == 400
        assert "DATA_UNAVAILABLE" in response.json()["detail"]
