"""Integration tests for the D034 broker-discovery endpoints, against a
real Postgres instance (same level as tests/api/test_admin_listings.py -
these routes read brokers/broker_grants directly).

The database is shared with every other test module and is not reset
between tests, so nothing here asserts on an absolute total row count.
Each test seeds the rows it owns and asserts on those. The central claim
under test is the scoping rule: a user sees exactly the brokers they hold
a grant for, and never another user's.
"""

import contextlib
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from apps.api.app.auth.security import hash_password
from apps.api.app.db.base import get_session
from apps.api.app.db.models import Broker, BrokerGrant, BrokerKind, User
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
async def plain_user(session):
    """A user with no role at all - broker discovery is authentication-only
    (D034), so this is the weakest identity that must still work."""
    user_id = uuid.uuid4()
    email = f"{uuid.uuid4()}@example.com"
    session.add(
        User(id=user_id, email=email, hashed_password=hash_password(TEST_PASSWORD), is_active=True)
    )
    await session.commit()
    try:
        yield user_id, email
    finally:
        await session.rollback()
        await session.execute(delete(BrokerGrant).where(BrokerGrant.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()


@contextlib.asynccontextmanager
async def broker_row(session, name: str, *, is_active: bool = False):
    broker_id = uuid.uuid4()
    session.add(
        Broker(
            id=broker_id,
            name=name,
            kind=BrokerKind.PAPER,
            provider="paper-sim",
            is_active=is_active,
        )
    )
    await session.commit()
    try:
        yield broker_id
    finally:
        await session.rollback()
        await session.execute(delete(BrokerGrant).where(BrokerGrant.broker_id == broker_id))
        await session.execute(delete(Broker).where(Broker.id == broker_id))
        await session.commit()


async def grant(session, user_id: uuid.UUID, broker_id: uuid.UUID) -> None:
    session.add(BrokerGrant(user_id=user_id, broker_id=broker_id))
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
    assert response.status_code == 200
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_listing_returns_only_the_callers_own_granted_brokers():
    """The core D034 claim: two users, different grants, each sees only
    their own - and neither sees the third broker nobody was granted."""
    async with db_session() as session:
        async with plain_user(session) as (user_a, email_a):
            async with plain_user(session) as (user_b, email_b):
                async with broker_row(session, "Alpha Paper") as broker_a:
                    async with broker_row(session, "Bravo Paper") as broker_b:
                        async with broker_row(session, "Charlie Paper") as broker_c:
                            await grant(session, user_a, broker_a)
                            await grant(session, user_b, broker_b)

                            async with api_client() as client:
                                token_a = await _get_token(client, email_a)
                                token_b = await _get_token(client, email_b)
                                list_a = await client.get(
                                    "/brokers?limit=500",
                                    headers={"Authorization": f"Bearer {token_a}"},
                                )
                                list_b = await client.get(
                                    "/brokers?limit=500",
                                    headers={"Authorization": f"Bearer {token_b}"},
                                )

                            assert list_a.status_code == 200
                            assert list_b.status_code == 200
                            ids_a = {row["id"] for row in list_a.json()["brokers"]}
                            ids_b = {row["id"] for row in list_b.json()["brokers"]}

                            assert str(broker_a) in ids_a
                            assert str(broker_b) not in ids_a
                            assert str(broker_b) in ids_b
                            assert str(broker_a) not in ids_b
                            # Granted to nobody: invisible to both.
                            assert str(broker_c) not in ids_a
                            assert str(broker_c) not in ids_b


@pytest.mark.asyncio
async def test_listing_row_shape_is_the_documented_fields_and_no_credentials():
    async with db_session() as session:
        async with plain_user(session) as (user_id, email):
            async with broker_row(session, "Shape Test Broker", is_active=True) as broker_id:
                await grant(session, user_id, broker_id)
                async with api_client() as client:
                    token = await _get_token(client, email)
                    response = await client.get(
                        "/brokers?limit=500", headers={"Authorization": f"Bearer {token}"}
                    )

                assert response.status_code == 200
                body = response.json()
                assert body["limit"] == 500
                assert body["offset"] == 0
                row = next(r for r in body["brokers"] if r["id"] == str(broker_id))
                assert set(row) == {"id", "name", "kind", "provider", "is_active"}
                assert row["name"] == "Shape Test Broker"
                assert row["kind"] == BrokerKind.PAPER.value
                assert row["provider"] == "paper-sim"
                assert row["is_active"] is True


@pytest.mark.asyncio
async def test_listing_is_empty_not_forbidden_for_a_user_with_no_grants():
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.get(
                "/brokers?limit=500", headers={"Authorization": f"Bearer {token}"}
            )
    assert response.status_code == 200
    assert response.json()["brokers"] == []


@pytest.mark.asyncio
async def test_listing_paginates_with_limit_and_offset_in_a_stable_order():
    async with db_session() as session, plain_user(session) as (user_id, email):
        async with broker_row(session, "Pager AAA") as first_id:
            async with broker_row(session, "Pager BBB") as second_id:
                await grant(session, user_id, first_id)
                await grant(session, user_id, second_id)
                async with api_client() as client:
                    token = await _get_token(client, email)
                    headers = {"Authorization": f"Bearer {token}"}
                    page1 = await client.get("/brokers?limit=1&offset=0", headers=headers)
                    page2 = await client.get("/brokers?limit=1&offset=1", headers=headers)
                    both = await client.get("/brokers?limit=2&offset=0", headers=headers)

                assert page1.status_code == page2.status_code == both.status_code == 200
                assert len(page1.json()["brokers"]) == 1
                assert page2.json()["offset"] == 1
                # The two single-row pages reassemble into the two-row page,
                # in order - that is what makes offset paging safe here.
                assert [
                    page1.json()["brokers"][0],
                    page2.json()["brokers"][0],
                ] == both.json()["brokers"]
                names = [r["name"] for r in both.json()["brokers"]]
                assert names == ["Pager AAA", "Pager BBB"]


@pytest.mark.asyncio
async def test_listing_rejects_a_limit_above_the_max():
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.get(
                "/brokers?limit=501", headers={"Authorization": f"Bearer {token}"}
            )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_listing_rejects_a_negative_offset():
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.get(
                "/brokers?offset=-1", headers={"Authorization": f"Bearer {token}"}
            )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_listing_is_401_without_a_token():
    async with api_client() as client:
        response = await client.get("/brokers")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_detail_returns_the_broker_for_a_user_holding_the_grant():
    async with db_session() as session, plain_user(session) as (user_id, email):
        async with broker_row(session, "Detail Broker") as broker_id:
            await grant(session, user_id, broker_id)
            async with api_client() as client:
                token = await _get_token(client, email)
                response = await client.get(
                    f"/brokers/{broker_id}", headers={"Authorization": f"Bearer {token}"}
                )
            assert response.status_code == 200
            body = response.json()
            assert body["id"] == str(broker_id)
            assert body["name"] == "Detail Broker"
            assert set(body) == {"id", "name", "kind", "provider", "is_active"}


@pytest.mark.asyncio
async def test_detail_is_403_when_the_broker_exists_but_the_caller_has_no_grant():
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with broker_row(session, "Ungranted Broker") as broker_id:
            async with api_client() as client:
                token = await _get_token(client, email)
                response = await client.get(
                    f"/brokers/{broker_id}", headers={"Authorization": f"Bearer {token}"}
                )
            assert response.status_code == 403
            assert str(broker_id) in response.json()["detail"]


@pytest.mark.asyncio
async def test_detail_is_404_for_a_broker_that_does_not_exist():
    missing = uuid.uuid4()
    async with db_session() as session, plain_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.get(
                f"/brokers/{missing}", headers={"Authorization": f"Bearer {token}"}
            )
    assert response.status_code == 404
    assert str(missing) in response.json()["detail"]


@pytest.mark.asyncio
async def test_detail_is_401_without_a_token():
    async with db_session() as session, plain_user(session) as (user_id, _email):
        async with broker_row(session, "No Token Broker") as broker_id:
            await grant(session, user_id, broker_id)
            async with api_client() as client:
                response = await client.get(f"/brokers/{broker_id}")
            assert response.status_code == 401
