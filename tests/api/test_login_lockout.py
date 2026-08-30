"""Real integration tests for the failed-login lockout (docs/DECISIONS.md D049).

These run against a real Postgres through the real route - the counter and
the lock timestamp are read back out of the `users` row, not out of an
in-memory double, because the whole point of D049's choice is that the
state is persisted.

The one thing that is NOT real here is the clock: lock EXPIRY is exercised
by substituting `apps.api.app.auth.lockout.utcnow`, so the suite proves the
expiry path in milliseconds instead of sleeping for the configured window.
The live end-to-end confirmation of expiry against a wall clock was done
separately with a short configured duration - see D049.
"""

import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from apps.api.app.auth import lockout
from apps.api.app.core.config import get_settings
from apps.api.app.db.models import User
from apps.api.app.main import app
from tests.api.test_auth import TEST_PASSWORD, active_user, api_client, db_session

WRONG_PASSWORD = "definitely-not-the-password"


@contextlib.contextmanager
def lockout_settings(*, max_attempts: int = 3, duration_minutes: int = 15):
    """Override only the two lockout knobs, leaving every other setting (the
    JWT secret above all) exactly as the environment configured it."""
    base = get_settings()
    overridden = base.model_copy(
        update={
            "auth_max_failed_login_attempts": max_attempts,
            "auth_lockout_duration_minutes": duration_minutes,
        }
    )
    app.dependency_overrides[get_settings] = lambda: overridden
    try:
        yield overridden
    finally:
        del app.dependency_overrides[get_settings]


@contextlib.contextmanager
def frozen_clock(moment: datetime):
    original = lockout.utcnow
    lockout.utcnow = lambda: moment
    try:
        yield
    finally:
        lockout.utcnow = original


async def _reload(session, user_id: uuid.UUID) -> User:
    await session.commit()  # drop this session's snapshot before re-reading
    return (await session.execute(select(User).where(User.id == user_id))).scalar_one()


async def _login(client, email: str, password: str):
    return await client.post("/auth/login", data={"username": email, "password": password})


@pytest.mark.asyncio
async def test_n_failed_attempts_lock_the_account():
    async with db_session() as session, active_user(session) as (user_id, email):
        with lockout_settings(max_attempts=3):
            async with api_client() as client:
                for _ in range(3):
                    assert (await _login(client, email, WRONG_PASSWORD)).status_code == 401

        user = await _reload(session, user_id)
        assert user.failed_login_count == 3
        assert user.locked_until is not None
        assert user.locked_until > datetime.now(UTC)


@pytest.mark.asyncio
async def test_correct_password_is_refused_while_locked():
    async with db_session() as session, active_user(session) as (user_id, email):
        with lockout_settings(max_attempts=3):
            async with api_client() as client:
                for _ in range(3):
                    await _login(client, email, WRONG_PASSWORD)

                response = await _login(client, email, TEST_PASSWORD)

    assert response.status_code == 423
    detail = response.json()["detail"]
    assert "locked" in detail.lower()
    # The lockout message must not be confusable with a wrong password.
    assert "incorrect" not in detail.lower()
    assert user_id is not None


@pytest.mark.asyncio
async def test_wrong_password_while_locked_still_reads_as_401_not_423():
    """The anti-enumeration property: 423 is only ever shown to a caller who
    already supplied the correct password, so it can never tell an attacker
    that an email is registered."""
    async with db_session() as session, active_user(session) as (user_id, email):
        with lockout_settings(max_attempts=3):
            async with api_client() as client:
                for _ in range(3):
                    await _login(client, email, WRONG_PASSWORD)

                response = await _login(client, email, WRONG_PASSWORD)

        assert response.status_code == 401
        # ...and the extra guess did not extend the lock.
        user = await _reload(session, user_id)
        assert user.failed_login_count == 3


@pytest.mark.asyncio
async def test_unknown_email_never_locks_or_leaks():
    with lockout_settings(max_attempts=3):
        async with api_client() as client:
            for _ in range(5):
                response = await _login(client, f"{uuid.uuid4()}@example.com", WRONG_PASSWORD)
                assert response.status_code == 401


@pytest.mark.asyncio
async def test_success_before_the_threshold_resets_the_counter():
    async with db_session() as session, active_user(session) as (user_id, email):
        with lockout_settings(max_attempts=3):
            async with api_client() as client:
                for _ in range(2):
                    await _login(client, email, WRONG_PASSWORD)
                assert (await _reload(session, user_id)).failed_login_count == 2

                assert (await _login(client, email, TEST_PASSWORD)).status_code == 200
                user = await _reload(session, user_id)
                assert user.failed_login_count == 0
                assert user.locked_until is None

                # Two more failures must not lock: the run restarted at zero.
                for _ in range(2):
                    await _login(client, email, WRONG_PASSWORD)
                assert (await _login(client, email, TEST_PASSWORD)).status_code == 200


@pytest.mark.asyncio
async def test_lock_expires_after_the_configured_duration():
    start = datetime.now(UTC)
    async with db_session() as session, active_user(session) as (user_id, email):
        with lockout_settings(max_attempts=3, duration_minutes=15):
            async with api_client() as client:
                with frozen_clock(start):
                    for _ in range(3):
                        await _login(client, email, WRONG_PASSWORD)
                    assert (await _login(client, email, TEST_PASSWORD)).status_code == 423

                # Still locked one minute before the window closes.
                with frozen_clock(start + timedelta(minutes=14)):
                    assert (await _login(client, email, TEST_PASSWORD)).status_code == 423

                # Expired: the same correct password now works.
                with frozen_clock(start + timedelta(minutes=16)):
                    assert (await _login(client, email, TEST_PASSWORD)).status_code == 200

        user = await _reload(session, user_id)
        assert user.failed_login_count == 0
        assert user.locked_until is None


@pytest.mark.asyncio
async def test_failure_after_an_expired_lock_starts_a_fresh_run():
    start = datetime.now(UTC)
    async with db_session() as session, active_user(session) as (user_id, email):
        with lockout_settings(max_attempts=3, duration_minutes=15):
            async with api_client() as client:
                with frozen_clock(start):
                    for _ in range(3):
                        await _login(client, email, WRONG_PASSWORD)

                with frozen_clock(start + timedelta(minutes=16)):
                    await _login(client, email, WRONG_PASSWORD)
                    user = await _reload(session, user_id)
                    # 1 of 3, not 4 - an expired lock must not leave the
                    # account one typo away from being locked forever.
                    assert user.failed_login_count == 1
                    assert user.locked_until is None
                    assert (await _login(client, email, TEST_PASSWORD)).status_code == 200


@pytest.mark.asyncio
async def test_lockout_disabled_by_configuration_never_locks():
    async with db_session() as session, active_user(session) as (user_id, email):
        with lockout_settings(max_attempts=0):
            async with api_client() as client:
                for _ in range(6):
                    assert (await _login(client, email, WRONG_PASSWORD)).status_code == 401
                assert (await _login(client, email, TEST_PASSWORD)).status_code == 200

        user = await _reload(session, user_id)
        assert user.failed_login_count == 0
        assert user.locked_until is None
