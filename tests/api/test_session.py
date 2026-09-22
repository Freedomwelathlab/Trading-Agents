"""Integration tests for GET /auth/session (D031), against a real Postgres
instance - the endpoint resolves the caller through get_current_user, so
it needs a real user row like every other authenticated route's tests.
"""

import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from apps.api.app.auth.security import create_access_token, hash_password
from apps.api.app.core.config import get_settings
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
        await session.rollback()
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
    assert response.status_code == 200
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_session_reports_the_real_identity_and_expiry_of_the_presented_token():
    settings = get_settings()
    async with db_session() as session, active_user(session) as (user_id, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            before = datetime.now(UTC)
            response = await client.get(
                "/auth/session", headers={"Authorization": f"Bearer {token}"}
            )
            after = datetime.now(UTC)

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == str(user_id)
    assert body["email"] == email

    lifetime = timedelta(minutes=settings.jwt_access_token_expire_minutes)
    expires_at = datetime.fromisoformat(body["expires_at"])
    # The expiry must be the token's own `exp` claim, not a recomputed
    # guess: it has to fall inside the window the login call could have
    # produced it in.
    assert before + lifetime - timedelta(seconds=5) <= expires_at <= after + lifetime

    issued_at = datetime.fromisoformat(body["issued_at"])
    assert before - timedelta(seconds=5) <= issued_at <= after

    remaining = body["expires_in_seconds"]
    assert 0 < remaining <= int(lifetime.total_seconds())


@pytest.mark.asyncio
async def test_session_never_echoes_the_token_or_any_credential_material():
    async with db_session() as session, active_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.get(
                "/auth/session", headers={"Authorization": f"Bearer {token}"}
            )
    body = response.json()
    assert token not in response.text
    for forbidden in ("access_token", "token", "password", "hashed_password"):
        assert forbidden not in body


@pytest.mark.asyncio
async def test_session_is_401_without_a_token():
    async with api_client() as client:
        response = await client.get("/auth/session")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_session_is_401_for_a_malformed_token():
    async with api_client() as client:
        response = await client.get(
            "/auth/session", headers={"Authorization": "Bearer not-a-jwt"}
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_session_is_401_for_an_expired_token():
    """An already-expired token fails closed here exactly as it does on
    every other route - the endpoint reports on a session, it never
    accepts a dead one just to describe it."""
    settings = get_settings()
    async with db_session() as session, active_user(session) as (user_id, _email):
        expired = create_access_token(
            user_id,
            settings.model_copy(update={"jwt_access_token_expire_minutes": -1}),
        )
        async with api_client() as client:
            response = await client.get(
                "/auth/session", headers={"Authorization": f"Bearer {expired}"}
            )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_session_is_401_for_a_deactivated_user_holding_a_still_valid_token():
    """Same D016 property the rest of the API has: deactivation takes
    effect on the very next request, so the UI's session poll goes 401
    immediately rather than reporting time remaining on a token whose
    owner can no longer use it."""
    async with db_session() as session, active_user(session) as (user_id, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            ok = await client.get("/auth/session", headers={"Authorization": f"Bearer {token}"})
            assert ok.status_code == 200

            user = await session.get(User, user_id)
            assert user is not None
            user.is_active = False
            await session.commit()

            response = await client.get(
                "/auth/session", headers={"Authorization": f"Bearer {token}"}
            )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_refresh_issues_a_new_token_that_keeps_the_original_session_start():
    """Phase 84 (D100): a refresh slides the expiry but carries `orig_iat`
    so the hard session ceiling is measured from first login."""
    from apps.api.app.auth.security import decode_access_token_claims

    settings = get_settings()
    async with db_session() as session, active_user(session) as (_uid, email), api_client() as c:
        token = await _get_token(c, email)
        first = decode_access_token_claims(token, settings)

        r = await c.post("/auth/refresh", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        new_token = r.json()["access_token"]
        # Within the same second the two tokens can be byte-identical (iat
        # has second resolution); the claims are what matter.
        second = decode_access_token_claims(new_token, settings)
        assert second["orig_iat"] == first["orig_iat"]
        assert second["exp"] >= first["exp"]

        # The refreshed token works, and reports the wider permissions shape.
        r = await c.get("/auth/session", headers={"Authorization": f"Bearer {new_token}"})
        assert r.status_code == 200 and "permissions" in r.json()


@pytest.mark.asyncio
async def test_refresh_refuses_a_session_older_than_the_ceiling():
    settings = get_settings()
    async with db_session() as session, active_user(session) as (uid, _email), api_client() as c:
        old = create_access_token(
            uid, settings,
            orig_iat=datetime.now(UTC) - timedelta(hours=settings.jwt_max_session_hours + 1),
        )
        r = await c.post("/auth/refresh", headers={"Authorization": f"Bearer {old}"})
        assert r.status_code == 401
        assert "SESSION_MAX_AGE" in r.json()["detail"]


@pytest.mark.asyncio
async def test_refresh_without_a_valid_token_is_401():
    async with api_client() as c:
        assert (await c.post("/auth/refresh")).status_code == 401
        r = await c.post("/auth/refresh", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401
