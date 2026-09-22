"""Integration tests against a real Postgres instance - the endpoint wires
together the DB, auth, per-broker access grants, the paper broker
registry, and the risk engine, so this is the level that actually proves
the whole path works.

Uses httpx.AsyncClient + ASGITransport rather than FastAPI's TestClient:
TestClient runs the app in a separate thread with its own event loop, which
is incompatible with the module-level, loop-bound DB engine
(apps/api/app/db/base.py) once a test also touches that engine directly -
see docs/DECISIONS.md D007/D009. AsyncClient runs in-process on the same
event loop as the rest of the test, avoiding the mismatch entirely.
"""

import contextlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from apps.api.app.auth.permissions import Permission
from apps.api.app.auth.security import hash_password
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    Broker,
    BrokerAccount,
    BrokerGrant,
    BrokerKind,
    BrokerPosition,
    PortfolioSnapshotPositionRow,
    PortfolioSnapshotRow,
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
        await session.execute(
            delete(PortfolioSnapshotPositionRow).where(
                PortfolioSnapshotPositionRow.snapshot_id.in_(
                    select(PortfolioSnapshotRow.id).where(
                        PortfolioSnapshotRow.broker_id == broker_id
                    )
                )
            )
        )
        await session.execute(
            delete(PortfolioSnapshotRow).where(PortfolioSnapshotRow.broker_id == broker_id)
        )
        await session.execute(delete(BrokerGrant).where(BrokerGrant.broker_id == broker_id))
        await session.execute(delete(BrokerPosition).where(BrokerPosition.broker_id == broker_id))
        await session.execute(delete(BrokerAccount).where(BrokerAccount.broker_id == broker_id))
        await session.execute(delete(Broker).where(Broker.id == broker_id))
        await session.commit()


@contextlib.asynccontextmanager
async def active_user(
    session,
    *,
    is_active: bool = True,
    permissions: tuple[str, ...] | None = (Permission.SUBMIT_PAPER_TRADE.value,),
):
    """`permissions=None` yields a user with no role at all (the 403 case).
    `permissions=()` yields a user with a role that grants nothing."""
    user_id = uuid.uuid4()
    email = f"{uuid.uuid4()}@example.com"
    role_id = None
    if permissions is not None:
        role_id = uuid.uuid4()
        session.add(Role(id=role_id, name=f"test-role-{role_id}", permissions=list(permissions)))
    session.add(
        User(
            id=user_id,
            email=email,
            hashed_password=hash_password(TEST_PASSWORD),
            is_active=is_active,
            role_id=role_id,
        )
    )
    await session.commit()
    try:
        yield user_id, email
    finally:
        await session.execute(delete(BrokerGrant).where(BrokerGrant.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        if role_id is not None:
            await session.execute(delete(Role).where(Role.id == role_id))
        await session.commit()


@contextlib.asynccontextmanager
async def broker_grant(session, *, user_id: uuid.UUID, broker_id: uuid.UUID):
    """Entered last / exits first relative to active_user and
    paper_broker_row, so the grant row is deleted before the user/broker
    rows it references."""
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
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
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
        broker_grant(session, user_id=user_id, broker_id=broker_id),
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
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
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
async def test_a_user_with_no_role_gets_403_not_401():
    async with (
        db_session() as session,
        active_user(session, permissions=None) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
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
    assert response.status_code == 403
    assert "trade:submit:paper" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_role_without_the_trade_permission_gets_403():
    async with (
        db_session() as session,
        active_user(session, permissions=()) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
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
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_a_user_with_the_permission_but_no_grant_for_this_broker_gets_403():
    async with (
        db_session() as session,
        active_user(session) as (_user_id, email),
        paper_broker_row(session) as broker_id,
        # deliberately no broker_grant() here
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
    assert response.status_code == 403
    assert "No access grant" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_grant_for_one_broker_does_not_authorize_a_different_broker():
    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as granted_broker_id,
        paper_broker_row(session) as other_broker_id,
        broker_grant(session, user_id=user_id, broker_id=granted_broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                f"/brokers/{other_broker_id}/trades",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_an_inactive_user_cannot_trade_even_with_a_previously_valid_token():
    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
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
async def test_live_broker_is_rejected_on_the_default_configuration():
    """Phase 43 (D058) built the live CODE path but left it inert: with the
    repository defaults (LIVE_TRADING_ENABLED=false) a live-broker request
    is still refused. The refusal now names the confirmation requirement
    first, because that gate is checked before the configuration one - see
    `_authorize_live_trade`. The full set of live-path behaviours lives in
    tests/api/test_live_trades.py."""
    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
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
        assert "LIVE_CONFIRMATION_REQUIRED" in response.json()["detail"]


@pytest.mark.asyncio
async def test_emergency_stop_blocks_the_trade():
    from apps.api.app.core.config import get_settings

    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        # D039: once ANY row exists in emergency_stop_events the persisted
        # state is authoritative and `settings.emergency_stop_active` is
        # only a bootstrap default. A shared dev database always has rows
        # by the time this runs, so flip the real switch rather than the
        # default that is no longer consulted.
        from apps.api.app.safety.emergency_stop import set_emergency_stop

        settings = get_settings()
        original = settings.emergency_stop_active
        settings.emergency_stop_active = True
        await set_emergency_stop(session, active=True, reason="test", actor_user_id=None)
        await session.commit()
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
            await set_emergency_stop(
                session, active=False, reason="test teardown", actor_user_id=None
            )
            await session.commit()

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "rejected"
        assert body["block_reason"] == "emergency_stop_active"


@pytest.mark.asyncio
async def test_missing_mark_for_an_existing_position_is_a_400_not_a_guess():
    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
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


@pytest.mark.asyncio
async def test_omitting_estimated_price_with_no_vendor_wired_is_400_not_configured():
    """The absent vendor is stated EXPLICITLY, not inherited from the
    environment (Phase 73, D091).

    This test used to rely on the test host simply having no Longbridge
    credentials, so `market_data_router` was already None. That held for
    as long as the platform had never been configured with any - and
    stopped holding the moment real credentials were added to `.env`,
    at which point the endpoint returned a genuine quote and the test
    failed for a reason that had nothing to do with the behaviour it
    describes.

    A test of "what happens when no vendor is wired" must wire no vendor
    itself. Now it means the same thing on a machine with credentials and
    on one without.
    """
    from apps.api.app.api.dependencies import get_market_data_router

    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            app.dependency_overrides[get_market_data_router] = lambda: None
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "AAPL",
                        "side": "buy",
                        "quantity": "10",
                        "stop_price": "95",
                    },
                )
            finally:
                del app.dependency_overrides[get_market_data_router]

        assert response.status_code == 400
        assert "NOT_CONFIGURED" in response.json()["detail"]


@pytest.mark.asyncio
async def test_omitting_estimated_price_uses_a_live_quote_from_the_vendor():
    from apps.api.app.api.dependencies import get_market_data_router
    from apps.api.app.marketdata.models import MarketSnapshot
    from apps.api.app.marketdata.router import MarketDataRouter

    class FakeProvider:
        name = "fake-vendor"

        async def get_snapshot(self, symbol: str) -> MarketSnapshot:
            return MarketSnapshot(
                symbol=symbol,
                price=Decimal("123.45"),
                as_of=datetime.now(UTC),
                source=self.name,
            )

    fake_router = MarketDataRouter([FakeProvider()])

    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            app.dependency_overrides[get_market_data_router] = lambda: fake_router
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"symbol": "AAPL", "side": "buy", "quantity": "10", "stop_price": "95"},
                )
            finally:
                del app.dependency_overrides[get_market_data_router]

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "filled"
        assert body["fill_price"] == "123.45"


@pytest.mark.asyncio
async def test_omitting_estimated_price_with_no_data_for_symbol_is_400_no_data_available():
    from apps.api.app.api.dependencies import get_market_data_router
    from apps.api.app.marketdata.models import MarketSnapshot
    from apps.api.app.marketdata.provider import DataUnavailableError
    from apps.api.app.marketdata.router import MarketDataRouter

    class FailingProvider:
        name = "fake-vendor"

        async def get_snapshot(self, symbol: str) -> MarketSnapshot:
            raise DataUnavailableError(f"no data for {symbol!r}")

    fake_router = MarketDataRouter([FailingProvider()])

    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            app.dependency_overrides[get_market_data_router] = lambda: fake_router
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"symbol": "AAPL", "side": "buy", "quantity": "10", "stop_price": "95"},
                )
            finally:
                del app.dependency_overrides[get_market_data_router]

        assert response.status_code == 400
        assert "NO_DATA_AVAILABLE" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_supplied_estimated_price_is_authoritative_even_with_a_vendor_wired():
    """If the caller supplies estimated_price, the vendor must not be
    consulted at all - proven here with a vendor that would raise if
    called."""
    from apps.api.app.api.dependencies import get_market_data_router
    from apps.api.app.marketdata.models import MarketSnapshot
    from apps.api.app.marketdata.router import MarketDataRouter

    class ExplodingProvider:
        name = "fake-vendor"

        async def get_snapshot(self, symbol: str) -> MarketSnapshot:
            raise AssertionError("vendor should not be consulted when a price is supplied")

    fake_router = MarketDataRouter([ExplodingProvider()])

    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            app.dependency_overrides[get_market_data_router] = lambda: fake_router
            try:
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
                del app.dependency_overrides[get_market_data_router]

        assert response.status_code == 200
        assert response.json()["fill_price"] == "100"


@pytest.mark.asyncio
async def test_an_identical_trade_submitted_twice_in_a_row_is_blocked_as_a_duplicate():
    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            payload = {
                "symbol": "AAPL",
                "side": "buy",
                "quantity": "10",
                "estimated_price": "100",
                "stop_price": "95",
            }
            first = await client.post(
                f"/brokers/{broker_id}/trades", headers=headers, json=payload
            )
            second = await client.post(
                f"/brokers/{broker_id}/trades", headers=headers, json=payload
            )

        assert first.status_code == 200
        assert first.json()["status"] == "filled"

        assert second.status_code == 200
        second_body = second.json()
        assert second_body["status"] == "rejected"
        assert second_body["approved"] is False
        assert second_body["block_reason"] == "duplicate_order"
        assert second_body["fill_price"] is None


@pytest.mark.asyncio
async def test_a_different_quantity_right_after_is_not_treated_as_a_duplicate():
    async with (
        db_session() as session,
        active_user(session) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            first = await client.post(
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
            second = await client.post(
                f"/brokers/{broker_id}/trades",
                headers=headers,
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "5",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )

        assert first.status_code == 200
        assert first.json()["status"] == "filled"
        assert second.status_code == 200
        assert second.json()["status"] == "filled"
