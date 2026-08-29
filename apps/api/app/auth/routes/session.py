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
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

from apps.api.app.auth.dependencies import get_current_user
from apps.api.app.auth.security import InvalidTokenError, decode_access_token_claims
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
    )
