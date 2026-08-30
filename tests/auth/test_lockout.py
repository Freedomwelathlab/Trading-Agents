"""Unit tests for the pure lockout arithmetic (docs/DECISIONS.md D049).

No DB, no app - these pin the state machine itself. The route-level
behaviour is covered against real Postgres in tests/api/test_login_lockout.py.
"""

from datetime import UTC, datetime, timedelta

from apps.api.app.auth.lockout import (
    LockoutState,
    is_locked,
    lockout_enabled,
    state_after_failed_attempt,
    state_after_successful_attempt,
)
from apps.api.app.core.config import Settings

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _settings(*, max_attempts: int = 3, duration_minutes: int = 15) -> Settings:
    return Settings(
        _env_file=None,
        jwt_secret_key="test-only-secret",
        auth_max_failed_login_attempts=max_attempts,
        auth_lockout_duration_minutes=duration_minutes,
    )


def test_no_lock_recorded_is_not_locked():
    assert not is_locked(LockoutState(0, None), NOW)


def test_future_lock_is_locked():
    assert is_locked(LockoutState(3, NOW + timedelta(minutes=1)), NOW)


def test_past_lock_is_not_locked():
    assert not is_locked(LockoutState(3, NOW - timedelta(seconds=1)), NOW)


def test_naive_lock_timestamp_is_read_as_utc_rather_than_raising():
    naive = (NOW + timedelta(minutes=5)).replace(tzinfo=None)
    assert is_locked(LockoutState(3, naive), NOW)


def test_failure_below_threshold_only_increments():
    state = state_after_failed_attempt(LockoutState(1, None), NOW, _settings())
    assert state == LockoutState(2, None)


def test_failure_at_threshold_sets_the_lock():
    state = state_after_failed_attempt(LockoutState(2, None), NOW, _settings())
    assert state.failed_login_count == 3
    assert state.locked_until == NOW + timedelta(minutes=15)


def test_failure_after_an_expired_lock_restarts_the_run():
    expired = LockoutState(3, NOW - timedelta(minutes=1))
    assert state_after_failed_attempt(expired, NOW, _settings()) == LockoutState(1, None)


def test_success_clears_everything():
    assert state_after_successful_attempt() == LockoutState(0, None)


def test_disabled_lockout_is_a_no_op():
    settings = _settings(max_attempts=0)
    assert not lockout_enabled(settings)
    state = LockoutState(9, None)
    assert state_after_failed_attempt(state, NOW, settings) == state
