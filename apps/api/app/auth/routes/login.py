from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.auth import lockout
from apps.api.app.auth.lockout import LockoutState
from apps.api.app.auth.schemas import TokenResponse
from apps.api.app.auth.security import create_access_token, hash_password, verify_password
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.db.base import get_session
from apps.api.app.db.models import User

router = APIRouter(tags=["auth"])

# A real bcrypt hash of an unused, unguessable password - verified against
# on a nonexistent user so a login attempt takes roughly the same time
# whether the email exists or not (avoids a timing side-channel that would
# let an attacker enumerate registered emails). Generated once at import
# time, not hand-written, so it's always a syntactically valid hash.
_DUMMY_HASH = hash_password("not-a-real-password-used-only-for-timing-parity")

_INVALID_CREDENTIALS = HTTPException(
    status_code=401,
    detail="Incorrect email or password.",
    headers={"WWW-Authenticate": "Bearer"},
)

# 423 Locked, not 401: the credentials presented were correct, so calling
# them "incorrect" would be a lie, and a client (the D020 frontend included)
# cannot tell a user why login failed if the server refuses to say. The
# message names the cause and the remedy without naming the remaining time,
# which would be a free oracle on exactly when to resume guessing.
#
# This is only ever raised AFTER the password has been verified as correct -
# see the ordering in login() and docs/DECISIONS.md D049 for why. A wrong
# password against a locked account still gets the generic 401 above, so
# 423 never tells an attacker that an email is registered.
_ACCOUNT_LOCKED = HTTPException(
    status_code=423,
    detail=(
        "Account temporarily locked after repeated failed login attempts. "
        "Try again later."
    ),
    headers={"WWW-Authenticate": "Bearer"},
)


@router.post("/auth/login", response_model=TokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    """OAuth2PasswordRequestForm's `username` field carries the email - this
    is the standard FastAPI form shape, not a hint that usernames exist as
    a separate concept in this app."""
    user = (
        await session.execute(select(User).where(User.email == form_data.username))
    ).scalar_one_or_none()

    hash_to_check = user.hashed_password if user is not None else _DUMMY_HASH
    password_ok = verify_password(form_data.password, hash_to_check)

    now = lockout.utcnow()
    state = (
        LockoutState(failed_login_count=user.failed_login_count, locked_until=user.locked_until)
        if user is not None
        else LockoutState(failed_login_count=0, locked_until=None)
    )
    currently_locked = user is not None and lockout.is_locked(state, now)

    if user is None or not user.is_active or not password_ok:
        # Only a real, active account with a real wrong password advances the
        # counter. An unknown email has nothing to count against; an inactive
        # user cannot log in regardless; and a wrong password against an
        # already-locked account must not extend the lock, or an attacker
        # could hold a victim out indefinitely by guessing forever.
        if user is not None and user.is_active and not password_ok and not currently_locked:
            await _persist(
                session, user, lockout.state_after_failed_attempt(state, now, settings)
            )
        raise _INVALID_CREDENTIALS

    if currently_locked:
        raise _ACCOUNT_LOCKED

    if state.failed_login_count != 0 or state.locked_until is not None:
        await _persist(session, user, lockout.state_after_successful_attempt())

    token = create_access_token(user.id, settings)
    return TokenResponse(access_token=token)


async def _persist(session: AsyncSession, user: User, state: LockoutState) -> None:
    user.failed_login_count = state.failed_login_count
    user.locked_until = state.locked_until
    await session.commit()
