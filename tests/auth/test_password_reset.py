"""Unit tests for the pure password-reset arithmetic (docs/DECISIONS.md D063).

No database, no HTTP, no clock of their own - apps/api/app/auth/password_reset.py
is a set of total functions, and this file is what makes that property worth
having: expiry and single-use are proven here in milliseconds, and the
integration suite (tests/api/test_password_reset.py) is then free to test the
route rather than re-testing the arithmetic through it.
"""

from datetime import UTC, datetime, timedelta

from apps.api.app.auth import password_reset
from apps.api.app.core.config import Settings

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _settings(**overrides) -> Settings:
    """`_env_file=None` matches tests/test_config.py's convention so a
    developer's local .env cannot change what these assertions mean."""
    return Settings(
        _env_file=None,
        jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256",
        **overrides,
    )


def test_generated_tokens_are_unguessable_and_never_repeat():
    tokens = {password_reset.generate_reset_token() for _ in range(200)}
    assert len(tokens) == 200
    # token_urlsafe(32) is 43 characters of base64url. A shorter token here
    # would mean someone reduced TOKEN_BYTES without reading why it is 32.
    assert all(len(token) >= 43 for token in tokens)


def test_the_raw_token_is_not_recoverable_from_its_hash():
    token = password_reset.generate_reset_token()
    digest = password_reset.hash_reset_token(token)
    assert token not in digest
    assert len(digest) == 64  # matches the column width in migration 0013
    assert digest == password_reset.hash_reset_token(token)  # deterministic
    assert digest != password_reset.hash_reset_token(token + "x")


def test_expiry_is_stamped_from_the_configured_ttl():
    settings = _settings(auth_password_reset_token_ttl_minutes=30)
    assert password_reset.expiry_for(NOW, settings) == NOW + timedelta(minutes=30)


def test_an_unused_unexpired_token_is_redeemable():
    state = password_reset.TokenState(expires_at=NOW + timedelta(minutes=1), used_at=None)
    assert password_reset.is_redeemable(state, NOW) is True


def test_an_expired_token_is_not_redeemable():
    state = password_reset.TokenState(expires_at=NOW - timedelta(seconds=1), used_at=None)
    assert password_reset.is_redeemable(state, NOW) is False


def test_expiry_is_exclusive_at_the_boundary():
    """A token whose expires_at is exactly `now` is spent. Erring the other
    way would make the TTL "N minutes plus one tick", which is a small lie
    the email's stated duration would then not match."""
    state = password_reset.TokenState(expires_at=NOW, used_at=None)
    assert password_reset.is_redeemable(state, NOW) is False


def test_a_used_token_is_never_redeemable_even_before_it_expires():
    """Single-use beats not-yet-expired. This is the asymmetry with
    lockout.is_locked, where a stale timestamp IS a clean slate."""
    state = password_reset.TokenState(
        expires_at=NOW + timedelta(hours=99), used_at=NOW - timedelta(minutes=1)
    )
    assert password_reset.is_redeemable(state, NOW) is False


def test_a_naive_expires_at_is_read_as_utc_rather_than_raising():
    """Postgres hands back aware datetimes, but a value written in the same
    transaction may not be. Comparing aware to naive raises TypeError, which
    on this path would be a 500 on a password reset."""
    naive = (NOW + timedelta(minutes=5)).replace(tzinfo=None)
    state = password_reset.TokenState(expires_at=naive, used_at=None)
    assert password_reset.is_redeemable(state, NOW) is True


def test_the_reset_link_carries_the_token_as_a_query_parameter():
    settings = _settings(auth_password_reset_url_base="https://ops.example.com/reset-password")
    link = password_reset.build_reset_link("abc-123", settings)
    assert link == "https://ops.example.com/reset-password?token=abc-123"


def test_the_reset_link_percent_encodes_a_token_with_url_metacharacters():
    """token_urlsafe never emits these, but the encoder must not be the
    thing standing between a future token format and a broken link."""
    settings = _settings(auth_password_reset_url_base="https://ops.example.com/reset-password/")
    link = password_reset.build_reset_link("a+b/c=d&e", settings)
    assert link == "https://ops.example.com/reset-password?token=a%2Bb%2Fc%3Dd%26e"


def test_the_email_body_states_the_real_configured_ttl():
    settings = _settings(auth_password_reset_token_ttl_minutes=45)
    body = password_reset.build_reset_email_body("https://x.test/r?token=t", settings)
    assert "https://x.test/r?token=t" in body
    assert "45 minutes" in body
    # It must not tell a user who did not request this to "ignore" a mail
    # while implying something already happened to their account.
    assert "Your password has not changed." in body
