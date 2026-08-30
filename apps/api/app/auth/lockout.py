"""Failed-login lockout arithmetic for POST /auth/login (docs/DECISIONS.md D049).

Deliberately pure: no session, no request, no I/O, no clock of its own -
`now` is always passed in. The login route owns reading and writing the two
`users` columns this module reasons about (`failed_login_count`,
`locked_until`); everything here is a total function of (current state, now,
settings), which is what makes the expiry behaviour testable without
sleeping for fifteen real minutes.

No new dependency backs any of this - see docs/DECISIONS.md D049 for why a
Postgres-persisted counter was chosen over an in-process rate limiter, a
Redis client (provisioned in docker-compose.yml but still unwired), or a
rate-limiting library.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from apps.api.app.core.config import Settings


def utcnow() -> datetime:
    """The single clock seam for the lockout path. Tests substitute this
    (rather than freezing time globally) to exercise lock expiry, so the
    route stays free of any test-only branching."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class LockoutState:
    """The two persisted columns, together. Frozen because every transition
    below returns a NEW state - nothing here mutates a User in place, so a
    caller can compute the outcome and decide separately whether to write
    it."""

    failed_login_count: int
    locked_until: datetime | None


def _as_aware(moment: datetime | None) -> datetime | None:
    """Postgres `timestamptz` comes back tz-aware, but a value assigned in
    the same transaction (or by a test) may not be. Comparing an aware and a
    naive datetime raises TypeError, so normalise rather than risk a 500 on
    the login path; a naive value is interpreted as UTC, which is what every
    writer in this codebase actually stores."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def is_locked(state: LockoutState, now: datetime) -> bool:
    """True only while a lock is actually in force. A `locked_until` in the
    past is NOT a lock - it is the residue of an expired one, and is treated
    as a clean slate rather than cleared eagerly (clearing it would mean a
    database write on every login attempt by every user who was ever
    locked)."""
    locked_until = _as_aware(state.locked_until)
    return locked_until is not None and locked_until > now


def lockout_enabled(settings: Settings) -> bool:
    return settings.auth_max_failed_login_attempts > 0


def state_after_failed_attempt(
    state: LockoutState, now: datetime, settings: Settings
) -> LockoutState:
    """Next state after one wrong password.

    An expired lock resets the run to zero before counting this failure, so
    a user who was locked, waited it out, and then mistyped once is at 1 of
    N - not at N and instantly re-locked, which would make the lockout
    permanent for anyone who ever tripped it once.
    """
    if not lockout_enabled(settings):
        return state

    lock_has_expired = state.locked_until is not None and not is_locked(state, now)
    previous = 0 if lock_has_expired else state.failed_login_count
    attempts = previous + 1

    if attempts >= settings.auth_max_failed_login_attempts:
        return LockoutState(
            failed_login_count=attempts,
            locked_until=now + timedelta(minutes=settings.auth_lockout_duration_minutes),
        )
    return LockoutState(failed_login_count=attempts, locked_until=None)


def state_after_successful_attempt() -> LockoutState:
    """A success always wipes the slate: the counter measures a CONSECUTIVE
    run of failures, so anything else would eventually lock out a user who
    simply logs in often enough to accumulate scattered typos."""
    return LockoutState(failed_login_count=0, locked_until=None)
