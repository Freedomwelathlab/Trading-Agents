"""The database-touching half of the password-reset flow
(docs/DECISIONS.md D063).

Two routes need exactly the same "issue a token for this user" behaviour -
the public self-service one and the admin one - and they must not drift
apart, because the difference between them is supposed to be who may call
them and what they are told back, NOT what gets written. So the write path
lives here once and both import it.

The pure arithmetic (hashing, expiry, redeemability) stays in
apps/api/app/auth/password_reset.py; this module is the part that needs a
session. Nothing here decides an HTTP status - callers own that, because
the two callers deliberately answer differently for the same outcome.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.auth import password_reset
from apps.api.app.core.config import Settings
from apps.api.app.db.models import PasswordResetToken, User

THROTTLE_WINDOW = timedelta(hours=1)
"""Window for the per-account limit. Deliberately the same length as
apps/api/app/auth/reset_throttle.py's per-IP window."""


@dataclass(frozen=True)
class IssuedToken:
    """What a successful issue produced. `raw_token` exists only in memory
    and in whatever the caller does with it next - it is never written
    anywhere, never logged, and never read back out of the database, which
    is the whole point of storing only its hash."""

    raw_token: str
    token_id: uuid.UUID
    expires_at: datetime


async def find_resettable_user(session: AsyncSession, email: str) -> User | None:
    """The user a reset may be issued for, or None.

    An INACTIVE user is None here, not a user: a deactivated account cannot
    log in (get_current_user and the login route both re-check `is_active`
    on every request), so issuing it a working password would be theatre -
    it would set a credential that still cannot be used, while telling the
    holder that it can.

    Callers must not let the difference between "no such email" and
    "inactive" reach a public response.
    """
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


async def recent_request_count(
    session: AsyncSession, user_id: uuid.UUID, now: datetime
) -> int:
    """How many tokens have been issued for this account inside the
    throttle window, counted from the rows themselves.

    Counting persisted rows rather than an in-process tally is what makes
    the per-account limit hold across workers and across restarts, at the
    cost of one indexed COUNT (see migration 0013's
    `ix_password_reset_tokens_user_created`). Used and expired rows still
    count: the limit is on how often a reset may be ASKED for, and a
    request that has already been redeemed consumed the same email send and
    the same write as one that has not.
    """
    return (
        await session.execute(
            select(func.count())
            .select_from(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.created_at > now - THROTTLE_WINDOW,
            )
        )
    ).scalar_one()


async def account_throttle_exceeded(
    session: AsyncSession, user_id: uuid.UUID, now: datetime, settings: Settings
) -> bool:
    limit = settings.auth_password_reset_max_requests_per_hour
    if limit <= 0:
        return False
    return await recent_request_count(session, user_id, now) >= limit


async def issue_token(
    session: AsyncSession, user: User, now: datetime, settings: Settings
) -> IssuedToken:
    """Create one reset token row and return the raw token.

    Does NOT invalidate the account's existing outstanding tokens. That is
    deliberate: a user who clicks "forgot password" twice because the first
    email was slow must not have the first link die the moment the second
    is issued - they cannot tell which of the two emails they are looking
    at. Outstanding tokens are instead all consumed at REDEMPTION time (see
    `redeem_token`), which is the moment the account is provably back under
    someone's control and old links stop being useful.

    Commits, because the caller's next act is an outbound email that cannot
    be rolled back: the row must exist before anything claims it does.
    """
    raw_token = password_reset.generate_reset_token()
    row = PasswordResetToken(
        user_id=user.id,
        token_hash=password_reset.hash_reset_token(raw_token),
        created_at=now,
        expires_at=password_reset.expiry_for(now, settings),
    )
    session.add(row)
    await session.commit()
    return IssuedToken(raw_token=raw_token, token_id=row.id, expires_at=row.expires_at)


async def invalidate_token(session: AsyncSession, token_id: uuid.UUID, now: datetime) -> None:
    """Consume a token without redeeming it. Used when an issue succeeded
    but the thing it was issued FOR did not (an email send that failed), so
    a live capability is never left dangling behind a 502 nobody acted on."""
    await session.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.id == token_id, PasswordResetToken.used_at.is_(None))
        .values(used_at=now)
    )
    await session.commit()


async def redeem_token(
    session: AsyncSession, raw_token: str, new_password_hash: str, now: datetime
) -> User | None:
    """Set the user's password from a valid token, or return None.

    None covers every failure identically - unknown token, expired token,
    already-used token, token whose user has since been deactivated - and
    the caller renders all of them as one `INVALID_OR_EXPIRED_TOKEN`. The
    distinctions are real but none of them are the caller's business: "this
    token existed but expired" confirms a real reset was requested for a
    real account.

    On success, three writes happen in one transaction:

      1. The password is replaced with `new_password_hash`.
      2. The login lockout is cleared (`failed_login_count`,
         `locked_until`). Someone who forgot their password very likely
         locked themselves out guessing at it first (D049), and leaving
         that lock in force would strand them behind a fresh, correct
         password for fifteen more minutes with no way to tell why.
      3. EVERY still-unused token for that user is marked consumed - the
         one just redeemed and any siblings. This is what makes an older
         link in an older email stop working, which matters most in exactly
         the case the flow exists for: an account someone else may also
         have requested a reset for.

    One transaction, so a crash between them cannot leave a changed
    password with a still-live token, or vice versa.
    """
    token_hash = password_reset.hash_reset_token(raw_token)
    row = (
        await session.execute(
            select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    state = password_reset.TokenState(expires_at=row.expires_at, used_at=row.used_at)
    if not password_reset.is_redeemable(state, now):
        return None

    user = (
        await session.execute(select(User).where(User.id == row.user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        return None

    user.hashed_password = new_password_hash
    user.failed_login_count = 0
    user.locked_until = None
    await session.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    await session.commit()
    return user
