"""get_current_user is the only sanctioned way for a route to learn who's
calling. Every route that isn't intentionally public must depend on it -
there is no other identity check in this codebase.

require_permission() builds on top of it for authorization: routes that
need more than "any authenticated active user" should depend on
require_permission(Permission.X) instead of get_current_user directly.
"""

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.api.app.auth.permissions import Permission
from apps.api.app.auth.security import InvalidTokenError, decode_access_token
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.db.base import get_session
from apps.api.app.db.models import User

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

_UNAUTHORIZED = HTTPException(
    status_code=401,
    detail="Not authenticated.",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    token: str | None = Depends(_oauth2_scheme),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> User:
    if token is None:
        raise _UNAUTHORIZED

    try:
        user_id = decode_access_token(token, settings)
    except InvalidTokenError:
        raise _UNAUTHORIZED from None

    user = (
        await session.execute(
            select(User).where(User.id == user_id).options(selectinload(User.role))
        )
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise _UNAUTHORIZED
    return user


def require_permission(permission: Permission) -> Callable[[User], Awaitable[User]]:
    """A user with no role, or a role that doesn't list this permission,
    gets 403 - distinct from get_current_user's 401, since the caller here
    is a real, authenticated identity that simply isn't allowed to do this,
    not an unidentified one."""

    async def checker(current_user: User = Depends(get_current_user)) -> User:
        role = current_user.role
        if role is None or permission.value not in role.permissions:
            raise HTTPException(
                status_code=403,
                detail=f"Missing required permission: {permission.value}",
            )
        return current_user

    return checker
