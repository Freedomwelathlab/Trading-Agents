"""Password hashing (bcrypt) and JWT access tokens. No password is ever
logged or stored anywhere but as a bcrypt hash - see
apps/api/app/core/logging.py's redaction for the log side of that.
"""

import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from apps.api.app.core.config import Settings


def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


class InvalidTokenError(Exception):
    """Covers missing, malformed, expired, or wrong-signature tokens alike
    - the caller (get_current_user) always responds with a generic 401
    regardless of which, so this doesn't need finer-grained subtypes."""


def create_access_token(user_id: uuid.UUID, settings: Settings) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token_claims(token: str, settings: Settings) -> dict:
    """Full verified claim set (`sub`/`iat`/`exp`). Signature and expiry are
    verified exactly as in decode_access_token - this is the same check,
    just without discarding everything but the subject. Used by
    GET /auth/session (D031) to report a real expiry to the UI; it never
    returns or echoes the token itself."""
    try:
        return dict(
            jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc


def decode_access_token(token: str, settings: Settings) -> uuid.UUID:
    payload = decode_access_token_claims(token, settings)

    subject = payload.get("sub")
    if subject is None:
        raise InvalidTokenError("Token has no subject claim.")
    try:
        return uuid.UUID(subject)
    except ValueError as exc:
        raise InvalidTokenError("Token subject is not a valid user id.") from exc
