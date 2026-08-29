"""Integration tests for the D030 admin listing endpoints, against a real
Postgres instance (same level as tests/api/test_admin.py - these routes
read users/roles/broker_grants directly).

The database is shared with every other test module and is not reset
between tests, so nothing here asserts on an absolute total row count.
Each test seeds rows it owns (uniquely named/emailed) and asserts those
rows appear, that pagination bounds are honoured, and that the row shape
never leaks a password hash.
"""

import contextlib
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from apps.api.app.auth.permissions import Permission
from apps.api.app.auth.security import hash_password
from apps.api.app.db.base import get_session
from apps.api.app.db.models import Broker, BrokerGrant, BrokerKind, Role, User
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
async def admin_user(session):
    role_id = uuid.uuid4()
    user_id = uuid.uuid4()
    email = f"{uuid.uuid4()}@example.com"
    session.add(
        Role(id=role_id, name=f"test-admin-{role_id}", permissions=[Permission.ADMIN.value])
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
        await session.rollback()
        await session.execute(delete(BrokerGrant).where(BrokerGrant.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.execute(delete(Role).where(Role.id == role_id))
        await session.commit()


@contextlib.asynccontextmanager
async def non_admin_user(session):
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
        await session.execute(delete(User).where(User.id == user_id))
        await session.commit()


@contextlib.asynccontextmanager
async def paper_broker_row(session):
    broker_id = uuid.uuid4()
    session.add(
        Broker(
            id=broker_id,
            name="Listing Test Broker",
            kind=BrokerKind.PAPER,
            provider="paper-sim",
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
async def test_listing_users_returns_the_real_seeded_user_without_any_password_field():
    async with db_session() as session, admin_user(session) as (admin_id, admin_email):
        async with api_client() as client:
            token = await _get_token(client, admin_email)
            response = await client.get(
                "/admin/users?limit=500", headers={"Authorization": f"Bearer {token}"}
            )
    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 500
    assert body["offset"] == 0

    listed = {row["id"]: row for row in body["users"]}
    assert str(admin_id) in listed
    row = listed[str(admin_id)]
    assert row["email"] == admin_email
    assert row["is_active"] is True
    # The one thing this endpoint must never do (D030 / schemas_admin.py).
    assert "password" not in row
    assert "hashed_password" not in row


@pytest.mark.asyncio
async def test_listing_users_is_ordered_by_email_and_paginates_with_limit_and_offset():
    # Needs at least two rows to prove paging at all - seed a second user
    # rather than assuming other tests left rows behind.
    async with db_session() as session, admin_user(session) as (_aid, admin_email):
        async with non_admin_user(session) as (_uid2, _email2), api_client() as client:
            token = await _get_token(client, admin_email)
            first = await client.get(
                "/admin/users?limit=1&offset=0", headers={"Authorization": f"Bearer {token}"}
            )
            second = await client.get(
                "/admin/users?limit=1&offset=1", headers={"Authorization": f"Bearer {token}"}
            )
            both = await client.get(
                "/admin/users?limit=2&offset=0", headers={"Authorization": f"Bearer {token}"}
            )

    assert first.status_code == second.status_code == both.status_code == 200
    assert len(first.json()["users"]) == 1
    assert second.json()["offset"] == 1
    # The two single-row pages must reassemble into the two-row page, in
    # order - that is what makes offset paging safe here.
    assert [first.json()["users"][0], second.json()["users"][0]] == both.json()["users"]
    emails = [row["email"] for row in both.json()["users"]]
    assert emails == sorted(emails)


@pytest.mark.asyncio
async def test_listing_users_rejects_a_limit_above_the_max():
    async with db_session() as session, admin_user(session) as (_aid, admin_email):
        async with api_client() as client:
            token = await _get_token(client, admin_email)
            response = await client.get(
                "/admin/users?limit=501", headers={"Authorization": f"Bearer {token}"}
            )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_listing_users_rejects_a_negative_offset():
    async with db_session() as session, admin_user(session) as (_aid, admin_email):
        async with api_client() as client:
            token = await _get_token(client, admin_email)
            response = await client.get(
                "/admin/users?offset=-1", headers={"Authorization": f"Bearer {token}"}
            )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_listing_roles_returns_the_seeded_role_with_its_real_permissions():
    async with db_session() as session, admin_user(session) as (_aid, admin_email):
        async with api_client() as client:
            token = await _get_token(client, admin_email)
            created = await client.post(
                "/admin/roles",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "name": f"listing-test-{uuid.uuid4()}",
                    "description": "created by the listing test",
                    "permissions": [Permission.SUBMIT_PAPER_TRADE.value],
                },
            )
            assert created.status_code == 201
            role_id = created.json()["id"]
            try:
                response = await client.get(
                    "/admin/roles?limit=500", headers={"Authorization": f"Bearer {token}"}
                )
            finally:
                await session.execute(delete(Role).where(Role.id == uuid.UUID(role_id)))
                await session.commit()

    assert response.status_code == 200
    body = response.json()
    listed = {row["id"]: row for row in body["roles"]}
    assert role_id in listed
    assert listed[role_id]["permissions"] == [Permission.SUBMIT_PAPER_TRADE.value]
    assert listed[role_id]["description"] == "created by the listing test"
    names = [row["name"] for row in body["roles"]]
    assert names == sorted(names)


@pytest.mark.asyncio
async def test_listing_broker_grants_surfaces_the_grant_id_needed_to_revoke_it():
    async with db_session() as session, admin_user(session) as (_aid, admin_email):
        async with non_admin_user(session) as (grantee_id, _ge):
            async with paper_broker_row(session) as broker_id:
                async with api_client() as client:
                    token = await _get_token(client, admin_email)
                    created = await client.post(
                        "/admin/broker-grants",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"user_id": str(grantee_id), "broker_id": str(broker_id)},
                    )
                    assert created.status_code == 201
                    grant_id = created.json()["id"]

                    listed = await client.get(
                        "/admin/broker-grants?limit=500",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert listed.status_code == 200
                    match = [g for g in listed.json()["grants"] if g["id"] == grant_id]
                    assert len(match) == 1
                    assert match[0]["user_id"] == str(grantee_id)
                    assert match[0]["broker_id"] == str(broker_id)

                    # The listed id is the real revoke handle, not a
                    # display-only value.
                    revoked = await client.delete(
                        f"/admin/broker-grants/{grant_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert revoked.status_code == 204

                    after = await client.get(
                        "/admin/broker-grants?limit=500",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    assert [g for g in after.json()["grants"] if g["id"] == grant_id] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/admin/users", "/admin/roles", "/admin/broker-grants"])
async def test_listings_are_403_for_a_user_without_admin_manage(path: str):
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert Permission.ADMIN.value in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/admin/users", "/admin/roles", "/admin/broker-grants"])
async def test_listings_are_401_without_a_token(path: str):
    async with api_client() as client:
        response = await client.get(path)
    assert response.status_code == 401
