"""Password-reset token arithmetic (docs/DECISIONS.md D063).

Deliberately pure, in the same spirit as apps/api/app/auth/lockout.py: no
session, no request, no I/O, and no clock of its own - `now` is always
passed in. The routes own reading and writing `password_reset_tokens`;
everything here is a total function of its arguments, which is what makes
the expiry and single-use rules testable without a database and without
sleeping for thirty real minutes.

The one exception to purity is `generate_reset_token`, which must draw
real entropy - there is no seam to substitute there and there should not
be one, because a test-substitutable token generator on the auth path is
exactly the kind of switch that survives into production.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from apps.api.app.core.config import Settings

TOKEN_BYTES = 32
"""256 bits of entropy per token. Sized so that guessing is not a threat
model this code has to defend against: at the per-IP throttle's default
ceiling an attacker gets on the order of 2^-250 per hour, so the real
attack surface is the email channel and the 30-minute window, not the
token space. The urlsafe-base64 encoding of 32 bytes is 43 characters,
which fits a query string without wrapping in any mail client."""


def generate_reset_token() -> str:
    """A fresh, unguessable token. `secrets`, never `random` - the latter
    is a Mersenne Twister whose full state is recoverable from a few
    hundred outputs, which for a password reset means recovering everyone
    else's links from your own."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_reset_token(token: str) -> str:
    """SHA-256 hex digest - 64 characters, matching the column width.

    Not bcrypt, and that is not a downgrade. Bcrypt's cost factor buys
    resistance to offline guessing of LOW-entropy secrets; this input has
    256 bits of entropy, so there is nothing to guess. Bcrypt would also
    make redemption unindexable: every hash carries its own salt, so
    "find the row for this token" would degrade from one indexed lookup to
    a bcrypt verification against every outstanding row.

    Plain (unsalted, unpeppered) is correct for the same reason: a rainbow
    table over a 256-bit random space cannot exist. What this hash defends
    against is exactly one thing - a database dump being a set of working
    reset links - and it defends against it completely.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    """The single clock seam for the reset path, same convention as
    apps/api/app/auth/lockout.py's. Tests substitute this to exercise
    expiry in milliseconds rather than branching the route on a test
    flag."""
    return datetime.now(UTC)


def expiry_for(now: datetime, settings: Settings) -> datetime:
    """Absolute expiry, computed once at issue and then stored. Deriving it
    at redemption time instead would let a later change to
    AUTH_PASSWORD_RESET_TOKEN_TTL_MINUTES silently extend or revoke links
    that are already in people's inboxes."""
    return now + timedelta(minutes=settings.auth_password_reset_token_ttl_minutes)


@dataclass(frozen=True)
class TokenState:
    """The two columns that decide redeemability, together. Frozen: nothing
    here mutates a row, so a caller can compute the verdict and decide
    separately whether to write anything."""

    expires_at: datetime
    used_at: datetime | None


def _as_aware(moment: datetime | None) -> datetime | None:
    """Postgres `timestamptz` reads back tz-aware, but a value written in
    the same transaction (or by a test) may not be, and comparing aware to
    naive raises TypeError. Normalise rather than risk a 500 on the reset
    path; a naive value is read as UTC, which is what every writer in this
    codebase actually stores. Same helper, same reasoning, as
    apps/api/app/auth/lockout.py's."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def is_redeemable(state: TokenState, now: datetime) -> bool:
    """A token is redeemable only while it is BOTH unused and unexpired.

    Note the asymmetry with lockout.is_locked: there, a stale timestamp is
    treated as a clean slate; here, any non-NULL `used_at` is permanent.
    Consumption is one-way on purpose - a token that could become valid
    again for any reason at all would not be single-use.
    """
    if state.used_at is not None:
        return False
    expires_at = _as_aware(state.expires_at)
    assert expires_at is not None  # column is NOT NULL; narrows for mypy
    return expires_at > now


def build_reset_link(token: str, settings: Settings) -> str:
    """The link handed to the user, either by email or (NOT_CONFIGURED) to
    an admin to relay. Built with an explicit `?token=` rather than a path
    segment so it is unambiguous which part is the secret, and so the
    frontend can read it with `useSearchParams` without a dynamic route."""
    return f"{settings.auth_password_reset_url_base.rstrip('/')}?token={quote(token, safe='')}"


RESET_EMAIL_SUBJECT = "Reset your Trading OS password"


def build_reset_email_body(link: str, settings: Settings) -> str:
    """Plain text, one link, no tracking pixel, no HTML alternative.

    States the real TTL rather than a rounded-off "shortly" - the number
    comes from the same setting that stamped the row, so the email cannot
    disagree with the database about how long the link lasts.
    """
    minutes = settings.auth_password_reset_token_ttl_minutes
    return (
        "A password reset was requested for your Trading OS account.\n\n"
        f"{link}\n\n"
        f"This link can be used once and expires in {minutes} minutes.\n\n"
        "If you did not request this, no action is needed - the link above "
        "is the only thing that was issued, and it will expire on its own. "
        "Your password has not changed."
    )
