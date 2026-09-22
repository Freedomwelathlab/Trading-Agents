"""GET /auth/session - who the current token belongs to and when it expires.

Exists because the frontend stores the JWT in an httpOnly cookie (D020),
which browser JavaScript cannot read by design. Without this endpoint the
UI has no truthful way to know how much session time is left, so a
"session expiring soon" warning could only ever be guessed from a
client-side timer started at login - a fabricated number the moment the
tab is restored, the clock skews, or the cookie was set in another tab.
See docs/DECISIONS.md D031.

Deliberately returns expiry metadata only - never the token, never the
password hash, never a refreshed token. There is no refresh/renewal
mechanism in this codebase (D010 issues one fixed-lifetime access token
and nothing else); this endpoint reports on that token, it does not
extend it.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

from apps.api.app.auth.dependencies import get_current_user
from apps.api.app.auth.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token_claims,
)
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.db.models import User

router = APIRouter(tags=["auth"])

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

_UNAUTHORIZED = HTTPException(
    status_code=401,
    detail="Not authenticated.",
    headers={"WWW-Authenticate": "Bearer"},
)


class SessionResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    issued_at: datetime | None
    expires_at: datetime
    permissions: list[str] = []
    """The permission strings the caller's role grants (Phase 84). Lets the
    UI decide what to show without guessing; the backend still enforces
    every permission on every request regardless of what the UI shows."""
    expires_in_seconds: int
    """Whole seconds remaining, computed server-side against the server's
    own clock, and floored at 0. The UI shows this rather than doing its
    own arithmetic on `expires_at`, so a skewed browser clock can't
    produce a warning that disagrees with what the API will actually
    accept."""


@router.get("/auth/session", response_model=SessionResponse)
async def get_session_info(
    token: str | None = Depends(_oauth2_scheme),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> SessionResponse:
    """401 for a missing, malformed, expired, or deactivated-user token -
    the same fail-closed behaviour every other authenticated route has,
    reached through the same get_current_user dependency (so a user
    deactivated a second ago is 401 here too, not "valid until expiry").
    """
    if token is None:
        raise _UNAUTHORIZED

    try:
        claims = decode_access_token_claims(token, settings)
    except InvalidTokenError:
        raise _UNAUTHORIZED from None

    exp = claims.get("exp")
    if exp is None:
        # Every token this codebase issues carries `exp` (see
        # create_access_token); a validly-signed one without it would mean
        # a token minted elsewhere, so fail closed rather than reporting a
        # session with no expiry.
        raise _UNAUTHORIZED

    expires_at = datetime.fromtimestamp(int(exp), tz=UTC)
    iat = claims.get("iat")
    issued_at = datetime.fromtimestamp(int(iat), tz=UTC) if iat is not None else None
    remaining = int((expires_at - datetime.now(UTC)).total_seconds())

    return SessionResponse(
        user_id=current_user.id,
        email=current_user.email,
        issued_at=issued_at,
        expires_at=expires_at,
        expires_in_seconds=max(remaining, 0),
        permissions=list(current_user.role.permissions or []) if current_user.role else [],
    )


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


@router.post("/auth/refresh", response_model=RefreshResponse)
async def refresh_session(
    token: str | None = Depends(_oauth2_scheme),
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> RefreshResponse:
    """Phase 84 (D100): a NEW token for a caller whose current one is still
    valid — the sliding half of the idle timeout. The web app calls this
    while the screen is active; it never calls it while idle, so an idle
    session expires on `jwt_access_token_expire_minutes` exactly as before.

    Two refusals: an expired/invalid token (401, same as everywhere — a
    dead session is not resurrected) and a session older than
    `jwt_max_session_hours` since first login (401 with a distinct
    detail), so "active" cannot mean "forever". A deactivated user is
    refused by `get_current_user` before this runs.
    """
    if token is None:
        raise _UNAUTHORIZED
    try:
        claims = decode_access_token_claims(token, settings)
    except InvalidTokenError:
        raise _UNAUTHORIZED from None

    orig = claims.get("orig_iat") or claims.get("iat")
    orig_dt = datetime.fromtimestamp(int(orig), tz=UTC) if orig is not None else datetime.now(UTC)
    if datetime.now(UTC) - orig_dt > timedelta(hours=settings.jwt_max_session_hours):
        raise HTTPException(
            status_code=401,
            detail=f"SESSION_MAX_AGE: signed in more than {settings.jwt_max_session_hours}h "
            f"ago; sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    new_token = create_access_token(current_user.id, settings, orig_iat=orig_dt)
    new_claims = decode_access_token_claims(new_token, settings)
    return RefreshResponse(
        access_token=new_token,
        expires_at=datetime.fromtimestamp(int(new_claims["exp"]), tz=UTC),
    )
