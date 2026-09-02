"""Real integration tests for POST /admin/users/{user_id}/password-reset
(docs/DECISIONS.md D063).

Same harness as tests/api/test_admin.py - a real Postgres, a real admin
role, a real bearer token from the real login route. The email provider is
the only substituted collaborator, and it implements the real
EmailProvider Protocol rather than being patched in at the call site.

This endpoint is the reason the feature is usable on a default checkout:
with no EMAIL_PROVIDER_* configured, it is the only way a reset link
reaches anybody. Both branches are therefore tested as first-class
behaviour, not as a fallback afterthought.
"""

import contextlib
import uuid

import pytest
from sqlalchemy import delete, select

from apps.api.app.auth.routes.password_reset import get_email_provider
from apps.api.app.db.models import PasswordResetToken, User
from apps.api.app.main import app
from tests.api.test_admin import (
    TEST_PASSWORD,
    _get_token,
    admin_user,
    api_client,
    db_session,
    non_admin_user,
)
from tests.api.test_password_reset import FakeEmailProvider


@contextlib.contextmanager
def email_provider(provider):
    app.dependency_overrides[get_email_provider] = lambda: provider
    try:
        yield provider
    finally:
        del app.dependency_overrides[get_email_provider]


async def _issue(client, token: str, user_id):
    return await client.post(
        f"/admin/users/{user_id}/password-reset",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _tokens_for(session, user_id: uuid.UUID):
    # commit + expire_all, not commit alone: the app's sessionmaker sets
    # expire_on_commit=False, so a plain re-select returns this session's
    # own identity-mapped objects instead of what the route wrote. See
    # tests/api/test_password_reset.py::_refresh.
    await session.commit()
    session.expire_all()
    return list(
        (
            await session.execute(
                select(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_a_non_admin_is_refused_with_403():
    """The permission gate, asserted against a real authenticated user who
    simply lacks admin:manage - not against an anonymous caller, which would
    only prove the 401 path."""
    async with db_session() as session, non_admin_user(session) as (uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await _issue(client, token, uid)

    assert response.status_code == 403
    assert "admin:manage" in response.json()["detail"]


@pytest.mark.asyncio
async def test_an_anonymous_caller_is_refused_with_401():
    async with api_client() as client:
        response = await client.post(f"/admin/users/{uuid.uuid4()}/password-reset")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_non_admin_never_receives_a_reset_link_in_the_403_body():
    """A permission failure must not leak the very capability it refused."""
    async with db_session() as session, non_admin_user(session) as (uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await _issue(client, token, uid)

    assert "reset_link" not in response.text
    assert "token=" not in response.text


@pytest.mark.asyncio
async def test_not_configured_returns_the_real_link_directly():
    """The headline NOT_CONFIGURED behaviour: no email vendor, so the link
    comes back in the response labelled for what it is, and an admin relays
    it by hand."""
    async with db_session() as session, admin_user(session) as (admin_id, admin_email):
        with email_provider(None):
            async with api_client() as client:
                token = await _get_token(client, admin_email)
                response = await _issue(client, token, admin_id)

        assert response.status_code == 201
        body = response.json()
        assert body["delivery"] == "NOT_CONFIGURED_returned_directly"
        assert body["reset_link"].startswith("http")
        assert "token=" in body["reset_link"]
        assert body["user_id"] == str(admin_id)
        assert body["expires_at"] is not None

        # The returned link is a REAL, redeemable capability, not a display
        # string - which is exactly why it is gated behind admin:manage.
        raw_token = body["reset_link"].split("token=", 1)[1]
        async with api_client() as client:
            confirm = await client.post(
                "/auth/password-reset/confirm",
                json={"token": raw_token, "new_password": "admin-chosen-new-pass-1"},
            )
        assert confirm.status_code == 200

        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == admin_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_a_configured_provider_sends_the_email_and_withholds_the_link():
    """Once email delivery exists, the API must stop handing live
    credentials back - otherwise configuring a provider would have changed
    nothing about who can obtain a link."""
    fake = FakeEmailProvider()
    async with db_session() as session, admin_user(session) as (admin_id, admin_email):
        with email_provider(fake):
            async with api_client() as client:
                token = await _get_token(client, admin_email)
                response = await _issue(client, token, admin_id)

        assert response.status_code == 201
        body = response.json()
        assert body["delivery"] == "SENT"
        assert body["reset_link"] is None
        assert "token=" not in response.text

        assert len(fake.sent) == 1
        assert fake.sent[0]["to"] == admin_email
        assert "token=" in fake.sent[0]["text"]

        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == admin_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_a_failed_send_is_a_502_and_invalidates_the_issued_token():
    """No 200-with-a-caveat: a send that failed is reported as a failure.
    The token is consumed on the way out so a failed attempt never leaves a
    live capability behind a response nobody acted on."""
    fake = FakeEmailProvider(fail=True)
    async with db_session() as session, admin_user(session) as (admin_id, admin_email):
        with email_provider(fake):
            async with api_client() as client:
                token = await _get_token(client, admin_email)
                response = await _issue(client, token, admin_id)

        assert response.status_code == 502
        detail = response.json()["detail"]
        assert detail.startswith("EMAIL_DELIVERY_FAILED")
        assert "invalidated" in detail
        assert "token=" not in response.text

        rows = await _tokens_for(session, admin_id)
        assert len(rows) == 1
        assert rows[0].used_at is not None

        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == admin_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_an_unknown_user_id_is_404():
    """The admin endpoint is allowed to be specific - the caller already
    holds admin:manage and named an id, so there is nothing to enumerate."""
    async with db_session() as session, admin_user(session) as (_uid, admin_email):
        with email_provider(None):
            async with api_client() as client:
                token = await _get_token(client, admin_email)
                response = await _issue(client, token, uuid.uuid4())

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_an_inactive_user_is_refused_rather_than_given_a_useless_link():
    """A deactivated account cannot log in, so a working password for it
    would be theatre. Saying so plainly is only safe here because the
    caller is already an admin."""
    async with db_session() as session, admin_user(session) as (_uid, admin_email):
        target_id = uuid.uuid4()
        target_email = f"{uuid.uuid4()}@example.com"
        async with api_client() as client:
            admin_token = await _get_token(client, admin_email)
            created = await client.post(
                "/admin/users",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "email": target_email,
                    "password": TEST_PASSWORD,
                    "is_active": False,
                },
            )
            assert created.status_code == 201
            target_id = uuid.UUID(created.json()["id"])

            with email_provider(None):
                response = await _issue(client, admin_token, target_id)

        try:
            assert response.status_code == 400
            assert response.json()["detail"].startswith("USER_INACTIVE")
            assert await _tokens_for(session, target_id) == []
        finally:
            await session.execute(
                delete(PasswordResetToken).where(PasswordResetToken.user_id == target_id)
            )
            await session.execute(delete(User).where(User.id == target_id))
            await session.commit()


@pytest.mark.asyncio
async def test_the_admin_endpoint_never_bypasses_single_use():
    """A link an admin handed over is the same kind of object the email
    flow issues - one use, then dead."""
    async with db_session() as session, admin_user(session) as (admin_id, admin_email):
        with email_provider(None):
            async with api_client() as client:
                token = await _get_token(client, admin_email)
                body = (await _issue(client, token, admin_id)).json()

        raw = body["reset_link"].split("token=", 1)[1]
        async with api_client() as client:
            first = await client.post(
                "/auth/password-reset/confirm",
                json={"token": raw, "new_password": "admin-chosen-new-pass-2"},
            )
            second = await client.post(
                "/auth/password-reset/confirm",
                json={"token": raw, "new_password": "admin-chosen-new-pass-3"},
            )

        assert first.status_code == 200
        assert second.status_code == 400
        assert second.json()["detail"].startswith("INVALID_OR_EXPIRED_TOKEN")

        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == admin_id)
        )
        await session.commit()
