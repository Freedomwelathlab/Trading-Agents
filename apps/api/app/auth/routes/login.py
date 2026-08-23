from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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

    if user is None or not user.is_active or not password_ok:
        raise _INVALID_CREDENTIALS

    token = create_access_token(user.id, settings)
    return TokenResponse(access_token=token)
