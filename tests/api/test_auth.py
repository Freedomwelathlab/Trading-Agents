import contextlib
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from apps.api.app.auth.security import hash_password
from apps.api.app.db.base import get_session
from apps.api.app.db.models import User
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


@pytest.mark.asyncio
async def test_correct_credentials_issue_a_bearer_token():
    async with db_session() as session, active_user(session) as (_user_id, email):
        async with api_client() as client:
            response = await client.post(
                "/auth/login", data={"username": email, "password": TEST_PASSWORD}
            )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) > 0


@pytest.mark.asyncio
async def test_wrong_password_is_rejected():
    async with db_session() as session, active_user(session) as (_user_id, email):
        async with api_client() as client:
            response = await client.post(
                "/auth/login", data={"username": email, "password": "wrong-password"}
            )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_unknown_email_is_rejected():
    async with api_client() as client:
        response = await client.post(
            "/auth/login",
            data={"username": "nobody@example.com", "password": "anything"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_inactive_user_cannot_log_in():
    async with db_session() as session, active_user(session, is_active=False) as (_id, email):
        async with api_client() as client:
            response = await client.post(
                "/auth/login", data={"username": email, "password": TEST_PASSWORD}
            )
    assert response.status_code == 401
