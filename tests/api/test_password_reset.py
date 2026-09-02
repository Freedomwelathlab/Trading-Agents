"""Real integration tests for the public password-reset flow
(docs/DECISIONS.md D063).

These run against a real Postgres through the real routes - the token rows,
the `used_at` transitions and the changed `users.hashed_password` are all
read back out of the database, because the properties under test (single
use, sibling invalidation, "the old password really stops working") are
statements about persisted state, not about a mock's call log.

Two things are substituted, both deliberately:

  * the clock (`password_reset.utcnow`), so expiry is exercised in
    milliseconds rather than by sleeping for thirty real minutes - same
    seam and same rationale as tests/api/test_login_lockout.py's;
  * the email provider, via a fake double implementing the real
    EmailProvider Protocol. Nothing in this suite sends mail, and no email
    credential exists anywhere in it.
"""

import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from apps.api.app.auth import password_reset
from apps.api.app.auth.reset_throttle import reset_request_throttle
from apps.api.app.auth.routes.password_reset import (
    ACKNOWLEDGEMENT,
    get_email_provider,
)
from apps.api.app.auth.security import verify_password
from apps.api.app.core.config import get_settings
from apps.api.app.db.models import PasswordResetToken, User
from apps.api.app.main import app
from apps.api.app.notifications.provider import EmailProviderError
from tests.api.test_auth import TEST_PASSWORD, active_user, api_client, db_session

NEW_PASSWORD = "a-brand-new-password-99"


class FakeEmailProvider:
    """Implements the real EmailProvider Protocol. Records what it was asked
    to send; optionally fails, so the failure branch is exercised against
    the same interface the real adapter satisfies."""

    name = "fake-email"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[dict[str, str]] = []

    async def send(self, *, to: str, subject: str, text: str) -> None:
        if self.fail:
            raise EmailProviderError("fake provider refused the message")
        self.sent.append({"to": to, "subject": subject, "text": text})


@contextlib.contextmanager
def email_provider(provider):
    """None here means NOT_CONFIGURED, which is the committed default and
    therefore the branch most deployments actually run."""
    app.dependency_overrides[get_email_provider] = lambda: provider
    try:
        yield provider
    finally:
        del app.dependency_overrides[get_email_provider]


@contextlib.contextmanager
def reset_settings(**overrides):
    """Override only the reset knobs, leaving every other setting (the JWT
    secret above all) exactly as the environment configured it - same
    pattern as tests/api/test_login_lockout.py's."""
    overridden = get_settings().model_copy(update=overrides)
    app.dependency_overrides[get_settings] = lambda: overridden
    try:
        yield overridden
    finally:
        del app.dependency_overrides[get_settings]


@contextlib.contextmanager
def frozen_clock(moment: datetime):
    original = password_reset.utcnow
    password_reset.utcnow = lambda: moment
    try:
        yield
    finally:
        password_reset.utcnow = original


@pytest.fixture(autouse=True)
def _clear_ip_throttle():
    """The per-IP throttle is a per-process singleton, and every test here
    shares one ASGI client address. Without this, the twentieth request in
    the file would start failing tests that have nothing to do with
    throttling."""
    reset_request_throttle.reset()
    yield
    reset_request_throttle.reset()


async def _request_reset(client, email: str):
    return await client.post("/auth/password-reset/request", json={"email": email})


async def _confirm_reset(client, token: str, new_password: str = NEW_PASSWORD):
    return await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": new_password}
    )


async def _refresh(session) -> None:
    """End this session's transaction AND drop its identity map before a
    re-read.

    `expire_on_commit=False` is set on the app's sessionmaker
    (apps/api/app/db/base.py), so a commit alone leaves already-loaded
    attributes in memory and a subsequent SELECT hands back the SAME stale
    object rather than the row the route just wrote from its own session.
    Every assertion in this file about persisted state would otherwise be
    an assertion about what the test itself put there.
    """
    await session.commit()
    session.expire_all()


async def _tokens_for(session, user_id: uuid.UUID) -> list[PasswordResetToken]:
    await _refresh(session)
    return list(
        (
            await session.execute(
                select(PasswordResetToken)
                .where(PasswordResetToken.user_id == user_id)
                .order_by(PasswordResetToken.created_at.asc())
            )
        )
        .scalars()
        .all()
    )


async def _reload_user(session, user_id: uuid.UUID) -> User:
    await _refresh(session)
    return (await session.execute(select(User).where(User.id == user_id))).scalar_one()


def _token_from(link: str) -> str:
    return link.split("token=", 1)[1]


# --------------------------------------------------------------------------
# The request endpoint: identical response in every branch
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_registered_email_gets_the_generic_acknowledgement():
    async with db_session() as session, active_user(session) as (user_id, email):
        with email_provider(None):
            async with api_client() as client:
                response = await _request_reset(client, email)

        assert response.status_code == 200
        assert response.json() == {"detail": ACKNOWLEDGEMENT}
        # ...and a real, redeemable token was actually created.
        tokens = await _tokens_for(session, user_id)
        assert len(tokens) == 1
        assert tokens[0].used_at is None
        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_an_unknown_email_gets_the_byte_identical_response():
    """The anti-enumeration property, asserted as an exact equality rather
    than a status check: any difference at all in the body - a different
    message, an extra field - would be the oracle this response exists to
    close."""
    with email_provider(None):
        async with api_client() as client:
            known_shape = await _request_reset(client, f"{uuid.uuid4()}@example.com")
            other = await _request_reset(client, f"{uuid.uuid4()}@example.com")

    assert known_shape.status_code == other.status_code == 200
    assert known_shape.json() == other.json() == {"detail": ACKNOWLEDGEMENT}


@pytest.mark.asyncio
async def test_registered_and_unregistered_emails_are_indistinguishable():
    async with db_session() as session, active_user(session) as (user_id, email):
        with email_provider(None):
            async with api_client() as client:
                registered = await _request_reset(client, email)
                unregistered = await _request_reset(client, f"{uuid.uuid4()}@example.com")

        assert registered.status_code == unregistered.status_code
        assert registered.json() == unregistered.json()
        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_an_unknown_email_writes_no_row_at_all():
    """Storage must not become the oracle the response refuses to be, and an
    unauthenticated endpoint must not let anyone fill a table with rows for
    addresses that can never redeem them."""
    async with db_session() as session:
        before = (
            await session.execute(select(PasswordResetToken.id))
        ).scalars().all()
        with email_provider(None):
            async with api_client() as client:
                for _ in range(3):
                    await _request_reset(client, f"{uuid.uuid4()}@example.com")
        await session.commit()
        after = (await session.execute(select(PasswordResetToken.id))).scalars().all()
    assert len(after) == len(before)


@pytest.mark.asyncio
async def test_an_inactive_user_gets_the_same_response_and_no_token():
    """A deactivated account cannot log in, so a reset for it would set a
    credential that still would not work - and saying so would leak that the
    address is registered."""
    async with db_session() as session, active_user(session, is_active=False) as (uid, email):
        with email_provider(None):
            async with api_client() as client:
                response = await _request_reset(client, email)

        assert response.status_code == 200
        assert response.json() == {"detail": ACKNOWLEDGEMENT}
        assert await _tokens_for(session, uid) == []


# --------------------------------------------------------------------------
# Delivery: NOT_CONFIGURED vs a configured provider
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_email_provider_still_issues_a_real_token_and_sends_nothing():
    async with db_session() as session, active_user(session) as (user_id, email):
        with email_provider(None):
            async with api_client() as client:
                response = await _request_reset(client, email)

        assert response.status_code == 200
        # The public response never carries the link, configured or not.
        assert "token" not in response.text
        assert "reset_link" not in response.text
        assert len(await _tokens_for(session, user_id)) == 1
        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_a_configured_provider_is_actually_called_with_a_working_link():
    fake = FakeEmailProvider()
    async with db_session() as session, active_user(session) as (user_id, email):
        with email_provider(fake):
            async with api_client() as client:
                assert (await _request_reset(client, email)).status_code == 200

                assert len(fake.sent) == 1
                assert fake.sent[0]["to"] == email
                # The emailed link must be redeemable - an email carrying a
                # link that does not work is worse than no email at all.
                link = [
                    word
                    for word in fake.sent[0]["text"].split()
                    if word.startswith("http")
                ][0]
                confirm = await _confirm_reset(client, _token_from(link))
                assert confirm.status_code == 200

        user = await _reload_user(session, user_id)
        assert verify_password(NEW_PASSWORD, user.hashed_password)
        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_a_failed_send_still_answers_the_same_generic_200():
    """It has to: varying the response on send failure would only ever vary
    it for registered addresses, which is the oracle again. The honest
    record of the failure is the structured log, not the response."""
    fake = FakeEmailProvider(fail=True)
    async with db_session() as session, active_user(session) as (user_id, email):
        with email_provider(fake):
            async with api_client() as client:
                response = await _request_reset(client, email)

        assert response.status_code == 200
        assert response.json() == {"detail": ACKNOWLEDGEMENT}
        # The token is deliberately left alive: this caller has no other way
        # to be handed a link, and the provider may have queued it anyway.
        tokens = await _tokens_for(session, user_id)
        assert len(tokens) == 1 and tokens[0].used_at is None
        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_the_acknowledgement_never_claims_an_email_was_sent():
    """The wording is load-bearing: it is true in all five branches
    (unknown, inactive, throttled, NOT_CONFIGURED, failed send), which an
    unconditional "we emailed you" would not be."""
    assert "has been issued" in ACKNOWLEDGEMENT
    assert "email delivery configured" in ACKNOWLEDGEMENT
    assert "we have sent" not in ACKNOWLEDGEMENT.lower()
    assert "has been sent to you" not in ACKNOWLEDGEMENT.lower()


# --------------------------------------------------------------------------
# The confirm endpoint
# --------------------------------------------------------------------------


async def _issue(client, email: str) -> str:
    """Drive the real request endpoint, then read the raw token out of the
    fake provider's captured email - the only place it legitimately exists
    outside the user's inbox. Deliberately NOT read out of the database:
    the database holds only a hash, and a test that could recover a token
    from it would be proving the storage is broken."""
    fake = FakeEmailProvider()
    with email_provider(fake):
        assert (await _request_reset(client, email)).status_code == 200
    link = [w for w in fake.sent[0]["text"].split() if w.startswith("http")][0]
    return _token_from(link)


@pytest.mark.asyncio
async def test_a_valid_token_changes_the_password_and_retires_the_old_one():
    async with db_session() as session, active_user(session) as (user_id, email):
        async with api_client() as client:
            token = await _issue(client, email)
            assert (await _confirm_reset(client, token)).status_code == 200

            # The new password works...
            assert (
                await client.post(
                    "/auth/login", data={"username": email, "password": NEW_PASSWORD}
                )
            ).status_code == 200
            # ...and the old one is genuinely gone, not merely deprioritised.
            assert (
                await client.post(
                    "/auth/login", data={"username": email, "password": TEST_PASSWORD}
                )
            ).status_code == 401

        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_a_token_cannot_be_redeemed_twice():
    async with db_session() as session, active_user(session) as (user_id, email):
        async with api_client() as client:
            token = await _issue(client, email)
            assert (await _confirm_reset(client, token)).status_code == 200

            second = await _confirm_reset(client, token, "yet-another-password-1")
            assert second.status_code == 400
            assert second.json()["detail"].startswith("INVALID_OR_EXPIRED_TOKEN")

        # The second attempt changed nothing.
        user = await _reload_user(session, user_id)
        assert verify_password(NEW_PASSWORD, user.hashed_password)
        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_an_expired_token_is_refused_with_the_same_sentinel():
    start = datetime.now(UTC)
    async with db_session() as session, active_user(session) as (user_id, email):
        with reset_settings(auth_password_reset_token_ttl_minutes=30):
            async with api_client() as client:
                with frozen_clock(start):
                    token = await _issue(client, email)

                # Still good one minute inside the window.
                with frozen_clock(start + timedelta(minutes=29)):
                    assert (
                        await client.post(
                            "/auth/password-reset/confirm",
                            json={"token": token + "x", "new_password": NEW_PASSWORD},
                        )
                    ).status_code == 400  # wrong token, window irrelevant

                with frozen_clock(start + timedelta(minutes=31)):
                    expired = await _confirm_reset(client, token)

        assert expired.status_code == 400
        assert expired.json()["detail"].startswith("INVALID_OR_EXPIRED_TOKEN")
        # The password was not changed by the expired attempt.
        user = await _reload_user(session, user_id)
        assert verify_password(TEST_PASSWORD, user.hashed_password)
        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_a_malformed_token_gets_the_same_sentinel_not_a_stack_trace():
    async with api_client() as client:
        for candidate in ["", "   ", "not-a-token", "../../etc/passwd", "%00", "a" * 511]:
            response = await client.post(
                "/auth/password-reset/confirm",
                json={"token": candidate, "new_password": NEW_PASSWORD},
            )
            # An empty token is a 422 from the schema's min_length; anything
            # else is the sentinel. Neither is ever a 500 or a traceback.
            assert response.status_code in (400, 422), candidate
            assert "Traceback" not in response.text
            if response.status_code == 400:
                assert response.json()["detail"].startswith("INVALID_OR_EXPIRED_TOKEN")


@pytest.mark.asyncio
async def test_the_rejection_says_nothing_about_why():
    """Distinguishing "expired" from "already used" from "never existed"
    would confirm that a real reset was requested for a real account."""
    async with db_session() as session, active_user(session) as (user_id, email):
        async with api_client() as client:
            token = await _issue(client, email)
            await _confirm_reset(client, token)
            used = await _confirm_reset(client, token, "another-password-42")
            unknown = await _confirm_reset(client, "definitely-not-a-real-token")

        assert used.status_code == unknown.status_code == 400
        assert used.json() == unknown.json()
        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_redeeming_one_token_invalidates_every_other_outstanding_one():
    """The property that matters when someone else may also have requested a
    reset for this account: the moment one link is used, every other live
    link for that user stops working."""
    async with db_session() as session, active_user(session) as (user_id, email):
        async with api_client() as client:
            first = await _issue(client, email)
            second = await _issue(client, email)
            assert first != second

            assert (await _confirm_reset(client, second)).status_code == 200

            replayed_first = await _confirm_reset(client, first, "third-password-77")
            assert replayed_first.status_code == 400
            assert replayed_first.json()["detail"].startswith("INVALID_OR_EXPIRED_TOKEN")

        rows = await _tokens_for(session, user_id)
        assert len(rows) == 2
        assert all(row.used_at is not None for row in rows)

        user = await _reload_user(session, user_id)
        assert verify_password(NEW_PASSWORD, user.hashed_password)
        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_a_short_password_is_refused_by_the_schema_not_silently_accepted():
    async with db_session() as session, active_user(session) as (user_id, email):
        async with api_client() as client:
            token = await _issue(client, email)
            assert (await _confirm_reset(client, token, "short")).status_code == 422
            # ...and the token survives, so the user can retry properly.
            assert (await _confirm_reset(client, token, "long-enough-password")).status_code == 200

        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_a_successful_reset_clears_a_login_lockout():
    """Someone who forgot their password very likely locked themselves out
    guessing at it first (D049). Leaving the lock in force would strand them
    behind a fresh, correct password with no way to tell why."""
    async with db_session() as session, active_user(session) as (user_id, email):
        async with api_client() as client:
            token = await _issue(client, email)

            user = await _reload_user(session, user_id)
            user.failed_login_count = 5
            user.locked_until = datetime.now(UTC) + timedelta(hours=1)
            await session.commit()

            assert (await _confirm_reset(client, token)).status_code == 200
            assert (
                await client.post(
                    "/auth/login", data={"username": email, "password": NEW_PASSWORD}
                )
            ).status_code == 200

        user = await _reload_user(session, user_id)
        assert user.failed_login_count == 0
        assert user.locked_until is None
        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
        await session.commit()


# --------------------------------------------------------------------------
# Throttling
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_per_account_limit_stops_issuing_tokens_without_changing_the_response():
    """The limit must be invisible from outside. A 429 that only fired for
    registered addresses would be a louder version of the oracle the generic
    200 exists to close."""
    async with db_session() as session, active_user(session) as (user_id, email):
        with reset_settings(auth_password_reset_max_requests_per_hour=2), email_provider(None):
            async with api_client() as client:
                responses = [await _request_reset(client, email) for _ in range(5)]

        assert [r.status_code for r in responses] == [200] * 5
        assert {r.text for r in responses} == {responses[0].text}
        # Only two rows were actually written.
        assert len(await _tokens_for(session, user_id)) == 2
        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_the_per_account_limit_can_be_disabled():
    async with db_session() as session, active_user(session) as (user_id, email):
        with reset_settings(auth_password_reset_max_requests_per_hour=0), email_provider(None):
            async with api_client() as client:
                for _ in range(6):
                    assert (await _request_reset(client, email)).status_code == 200

        assert len(await _tokens_for(session, user_id)) == 6
        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_the_per_ip_limit_answers_429_for_unknown_addresses_too():
    """This is the layer that bounds a flood of addresses that do not exist,
    which the per-account row count by construction cannot see. It is keyed
    on the client, never the email, so it reveals nothing."""
    with reset_settings(auth_password_reset_max_requests_per_ip_per_hour=3):
        async with api_client() as client:
            allowed = [
                await _request_reset(client, f"{uuid.uuid4()}@example.com") for _ in range(3)
            ]
            refused = await _request_reset(client, f"{uuid.uuid4()}@example.com")

    assert [r.status_code for r in allowed] == [200, 200, 200]
    assert refused.status_code == 429


@pytest.mark.asyncio
async def test_the_per_ip_limit_is_indifferent_to_whether_the_email_exists():
    async with db_session() as session, active_user(session) as (user_id, email):
        with reset_settings(auth_password_reset_max_requests_per_ip_per_hour=2):
            async with api_client() as client:
                await _request_reset(client, f"{uuid.uuid4()}@example.com")
                await _request_reset(client, f"{uuid.uuid4()}@example.com")
                # A REGISTERED address gets the same 429 - the throttle
                # cannot be used to probe which addresses are real.
                refused_real = await _request_reset(client, email)

        assert refused_real.status_code == 429
        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_the_reset_endpoints_require_no_authentication():
    """Stated as a test because it is the one property that makes this
    feature useful at all - a user who cannot log in cannot present a
    bearer token."""
    async with api_client() as client:
        response = await _request_reset(client, f"{uuid.uuid4()}@example.com")
        assert response.status_code != 401
        confirm = await _confirm_reset(client, "nope")
        assert confirm.status_code == 400  # rejected on the token, not on auth
