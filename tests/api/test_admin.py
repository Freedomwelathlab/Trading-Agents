"""Integration tests against a real Postgres instance - the /admin routes
touch users, roles, and broker_grants directly, so this is exercised at
the DB-backed integration level, same as the rest of the API tests.
"""

import contextlib
import uuid

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
async def admin_user(session):
    """An active user whose role grants Permission.ADMIN."""
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
        Broker(id=broker_id, name="Test Broker", kind=BrokerKind.PAPER, provider="paper-sim")
    )
    await session.commit()
    try:
        yield broker_id
    finally:
        await session.rollback()
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
async def test_non_admin_cannot_create_a_user():
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                "/admin/users",
                headers={"Authorization": f"Bearer {token}"},
                json={"email": "new@example.com", "password": "irrelevant123"},
            )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_create_a_user_and_the_user_can_immediately_log_in():
    new_email = f"{uuid.uuid4()}@example.com"
    async with db_session() as session, admin_user(session) as (_uid, admin_email):
        try:
            async with api_client() as client:
                admin_token = await _get_token(client, admin_email)
                create_response = await client.post(
                    "/admin/users",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"email": new_email, "password": "a-real-password-123"},
                )
                assert create_response.status_code == 201
                body = create_response.json()
                assert body["email"] == new_email
                assert "password" not in body
                assert "hashed_password" not in body

                login_response = await client.post(
                    "/auth/login", data={"username": new_email, "password": "a-real-password-123"}
                )
                assert login_response.status_code == 200
        finally:
            await session.execute(delete(User).where(User.email == new_email))
            await session.commit()


@pytest.mark.asyncio
async def test_creating_a_user_with_a_duplicate_email_is_409():
    async with db_session() as session, admin_user(session) as (_uid, admin_email):
        async with api_client() as client:
            admin_token = await _get_token(client, admin_email)
            response = await client.post(
                "/admin/users",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"email": admin_email, "password": "irrelevant123"},
            )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_admin_can_create_a_role():
    role_name = f"test-role-{uuid.uuid4()}"
    async with db_session() as session, admin_user(session) as (_uid, admin_email):
        try:
            async with api_client() as client:
                admin_token = await _get_token(client, admin_email)
                response = await client.post(
                    "/admin/roles",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={
                        "name": role_name,
                        "permissions": [Permission.SUBMIT_PAPER_TRADE.value],
                    },
                )
            assert response.status_code == 201
            body = response.json()
            assert body["name"] == role_name
            assert body["permissions"] == [Permission.SUBMIT_PAPER_TRADE.value]
        finally:
            await session.execute(delete(Role).where(Role.name == role_name))
            await session.commit()


@pytest.mark.asyncio
async def test_creating_a_role_with_a_duplicate_name_is_409():
    async with db_session() as session, admin_user(session) as (_uid, admin_email):
        async with api_client() as client:
            admin_token = await _get_token(client, admin_email)
            # First creation succeeds.
            role_name = f"dup-role-{uuid.uuid4()}"
            first = await client.post(
                "/admin/roles",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"name": role_name, "permissions": []},
            )
            assert first.status_code == 201
            try:
                second = await client.post(
                    "/admin/roles",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"name": role_name, "permissions": []},
                )
                assert second.status_code == 409
            finally:
                await session.execute(delete(Role).where(Role.name == role_name))
                await session.commit()


@pytest.mark.asyncio
async def test_admin_can_grant_and_revoke_broker_access_and_it_changes_trade_authorization():
    async with (
        db_session() as session,
        admin_user(session) as (_admin_id, admin_email),
        non_admin_user(session) as (trader_id, trader_email),
        paper_broker_row(session) as broker_id,
    ):
        role_id = uuid.uuid4()
        session.add(
            Role(
                id=role_id,
                name=f"trader-{role_id}",
                permissions=[Permission.SUBMIT_PAPER_TRADE.value],
            )
        )
        trader = (await session.execute(select(User).where(User.id == trader_id))).scalar_one()
        trader.role_id = role_id
        await session.commit()
        try:
            async with api_client() as client:
                admin_token = await _get_token(client, admin_email)
                trader_token = await _get_token(client, trader_email)
                trade_body = {
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "1",
                    "estimated_price": "100",
                    "stop_price": "95",
                }

                # Before any grant: 403.
                before = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {trader_token}"},
                    json=trade_body,
                )
                assert before.status_code == 403

                # Admin grants access.
                grant_response = await client.post(
                    "/admin/broker-grants",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"user_id": str(trader_id), "broker_id": str(broker_id)},
                )
                assert grant_response.status_code == 201
                grant_id = grant_response.json()["id"]

                # After the grant: trade succeeds.
                after_grant = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {trader_token}"},
                    json=trade_body,
                )
                assert after_grant.status_code == 200
                assert after_grant.json()["status"] == "filled"

                # Admin revokes access.
                revoke_response = await client.delete(
                    f"/admin/broker-grants/{grant_id}",
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                assert revoke_response.status_code == 204

                # After revocation: 403 again.
                after_revoke = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {trader_token}"},
                    json=trade_body,
                )
                assert after_revoke.status_code == 403
        finally:
            async with db_session() as cleanup_session:
                trader = (
                    await cleanup_session.execute(select(User).where(User.id == trader_id))
                ).scalar_one()
                trader.role_id = None
                await cleanup_session.commit()
                await cleanup_session.execute(delete(Role).where(Role.id == role_id))
                await cleanup_session.commit()


@pytest.mark.asyncio
async def test_creating_a_grant_for_an_unknown_user_is_404():
    async with (
        db_session() as session,
        admin_user(session) as (_uid, admin_email),
        paper_broker_row(session) as broker_id,
    ):
        async with api_client() as client:
            admin_token = await _get_token(client, admin_email)
            response = await client.post(
                "/admin/broker-grants",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"user_id": str(uuid.uuid4()), "broker_id": str(broker_id)},
            )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_creating_a_duplicate_grant_is_409():
    async with (
        db_session() as session,
        admin_user(session) as (_uid, admin_email),
        non_admin_user(session) as (trader_id, _trader_email),
        paper_broker_row(session) as broker_id,
    ):
        async with api_client() as client:
            admin_token = await _get_token(client, admin_email)
            headers = {"Authorization": f"Bearer {admin_token}"}
            body = {"user_id": str(trader_id), "broker_id": str(broker_id)}
            first = await client.post("/admin/broker-grants", headers=headers, json=body)
            assert first.status_code == 201
            second = await client.post("/admin/broker-grants", headers=headers, json=body)
            assert second.status_code == 409


@pytest.mark.asyncio
async def test_revoking_an_unknown_grant_is_404():
    async with db_session() as session, admin_user(session) as (_uid, admin_email):
        async with api_client() as client:
            admin_token = await _get_token(client, admin_email)
            response = await client.delete(
                f"/admin/broker-grants/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_admin_can_deactivate_a_user_and_it_takes_effect_immediately():
    async with db_session() as session, admin_user(session) as (_aid, admin_email):
        async with non_admin_user(session) as (target_id, target_email):
            async with api_client() as client:
                admin_token = await _get_token(client, admin_email)
                target_token = await _get_token(client, target_email)

                pre_check = await client.post(
                    "/admin/users",
                    headers={"Authorization": f"Bearer {target_token}"},
                    json={"email": "irrelevant@example.com", "password": "irrelevant123"},
                )
                assert pre_check.status_code == 403  # not admin, but authenticated (not 401)

                response = await client.patch(
                    f"/admin/users/{target_id}",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"is_active": False},
                )
                assert response.status_code == 200
                assert response.json()["is_active"] is False

                post_check = await client.post(
                    "/admin/users",
                    headers={"Authorization": f"Bearer {target_token}"},
                    json={"email": "irrelevant@example.com", "password": "irrelevant123"},
                )
                assert post_check.status_code == 401

                login_attempt = await client.post(
                    "/auth/login",
                    data={"username": target_email, "password": TEST_PASSWORD},
                )
                assert login_attempt.status_code == 401


@pytest.mark.asyncio
async def test_admin_can_assign_a_role_to_a_user_via_patch():
    role_name = f"assign-role-{uuid.uuid4()}"
    async with db_session() as session, admin_user(session) as (_aid, admin_email):
        async with non_admin_user(session) as (target_id, _target_email):
            try:
                async with api_client() as client:
                    admin_token = await _get_token(client, admin_email)
                    headers = {"Authorization": f"Bearer {admin_token}"}
                    create_role = await client.post(
                        "/admin/roles",
                        headers=headers,
                        json={
                            "name": role_name,
                            "permissions": [Permission.SUBMIT_PAPER_TRADE.value],
                        },
                    )
                    assert create_role.status_code == 201
                    new_role_id = create_role.json()["id"]

                    response = await client.patch(
                        f"/admin/users/{target_id}",
                        headers=headers,
                        json={"role_id": new_role_id},
                    )
                    assert response.status_code == 200
                    assert response.json()["role_id"] == new_role_id
            finally:
                # The PATCH above set target_id's role_id to new_role_id -
                # clear it before deleting the Role or the FK blocks it.
                target = (
                    await session.execute(select(User).where(User.id == target_id))
                ).scalar_one()
                target.role_id = None
                await session.commit()
                await session.execute(delete(Role).where(Role.name == role_name))
                await session.commit()


@pytest.mark.asyncio
async def test_assigning_an_unknown_role_via_patch_is_404():
    async with db_session() as session, admin_user(session) as (_aid, admin_email):
        async with non_admin_user(session) as (target_id, _target_email):
            async with api_client() as client:
                admin_token = await _get_token(client, admin_email)
                response = await client.patch(
                    f"/admin/users/{target_id}",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"role_id": str(uuid.uuid4())},
                )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_updating_an_unknown_user_is_404():
    async with db_session() as session, admin_user(session) as (_aid, admin_email):
        async with api_client() as client:
            admin_token = await _get_token(client, admin_email)
            response = await client.patch(
                f"/admin/users/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"is_active": False},
            )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_omitting_role_id_in_a_patch_leaves_it_untouched():
    role_name = f"untouched-role-{uuid.uuid4()}"
    async with db_session() as session, admin_user(session) as (_aid, admin_email):
        role_id = uuid.uuid4()
        session.add(Role(id=role_id, name=role_name, permissions=[]))
        await session.commit()
        async with non_admin_user(session) as (target_id, _target_email):
            target = (
                await session.execute(select(User).where(User.id == target_id))
            ).scalar_one()
            target.role_id = role_id
            await session.commit()
            try:
                async with api_client() as client:
                    admin_token = await _get_token(client, admin_email)
                    response = await client.patch(
                        f"/admin/users/{target_id}",
                        headers={"Authorization": f"Bearer {admin_token}"},
                        json={"is_active": True},
                    )
                assert response.status_code == 200
                assert response.json()["role_id"] == str(role_id)
            finally:
                target.role_id = None
                await session.commit()
                await session.execute(delete(Role).where(Role.id == role_id))
                await session.commit()


@pytest.mark.asyncio
async def test_updating_a_roles_permissions_changes_authorization_for_every_holder():
    role_name = f"mutable-role-{uuid.uuid4()}"
    async with (
        db_session() as session,
        admin_user(session) as (_aid, admin_email),
        non_admin_user(session) as (trader_id, trader_email),
        paper_broker_row(session) as broker_id,
    ):
        role_id = uuid.uuid4()
        session.add(Role(id=role_id, name=role_name, permissions=[]))
        trader = (await session.execute(select(User).where(User.id == trader_id))).scalar_one()
        trader.role_id = role_id
        await session.commit()
        try:
            session.add(BrokerGrant(user_id=trader_id, broker_id=broker_id))
            await session.commit()

            async with api_client() as client:
                admin_token = await _get_token(client, admin_email)
                trader_token = await _get_token(client, trader_email)
                trade_body = {
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "1",
                    "estimated_price": "100",
                    "stop_price": "95",
                }

                before = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {trader_token}"},
                    json=trade_body,
                )
                assert before.status_code == 403

                patch_response = await client.patch(
                    f"/admin/roles/{role_id}",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"permissions": [Permission.SUBMIT_PAPER_TRADE.value]},
                )
                assert patch_response.status_code == 200
                assert patch_response.json()["permissions"] == [Permission.SUBMIT_PAPER_TRADE.value]

                after = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {trader_token}"},
                    json=trade_body,
                )
                assert after.status_code == 200
                assert after.json()["status"] == "filled"
        finally:
            await session.execute(delete(BrokerGrant).where(BrokerGrant.user_id == trader_id))
            trader.role_id = None
            await session.commit()
            await session.execute(delete(Role).where(Role.id == role_id))
            await session.commit()


@pytest.mark.asyncio
async def test_updating_an_unknown_role_is_404():
    async with db_session() as session, admin_user(session) as (_aid, admin_email):
        async with api_client() as client:
            admin_token = await _get_token(client, admin_email)
            response = await client.patch(
                f"/admin/roles/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"description": "does not matter"},
            )
    assert response.status_code == 404
