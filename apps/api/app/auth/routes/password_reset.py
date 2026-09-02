"""Public, unauthenticated password-reset endpoints (docs/DECISIONS.md D063).

Two routes, and the asymmetry between them is the whole design:

  POST /auth/password-reset/request  - ALWAYS 200, always the same body.
  POST /auth/password-reset/confirm  - 200, or 400 with one sentinel.

The request endpoint is the only unauthenticated route in this app that
takes a user identifier, so it is the one place where an enumeration
oracle can be built. Every branch below - user not found, user inactive,
account throttled, email provider absent, email send failed - converges on
the same status and the same bytes. Any future edit that makes one of them
distinguishable reopens the hole, which is why the acknowledgement is a
module constant rather than a string literal per branch.

The confirm endpoint answers 400 `INVALID_OR_EXPIRED_TOKEN` for every
failure, with no elaboration. "Expired" and "already used" would both
confirm that a real reset was requested for a real account; the caller
cannot act differently on any of them anyway (the remedy is the same:
request a new link).
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.auth import password_reset, reset_service
from apps.api.app.auth.reset_throttle import reset_request_throttle
from apps.api.app.auth.schemas import (
    PasswordResetAcknowledgement,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
)
from apps.api.app.auth.security import hash_password
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_session
from apps.api.app.notifications.provider import EmailProvider, EmailProviderError

router = APIRouter(tags=["auth"])
logger = get_logger(__name__)

ACKNOWLEDGEMENT = (
    "If that email address is registered, a password reset link has been issued for it. "
    "The link can be used once and expires shortly. If no email arrives, contact an "
    "administrator - this deployment may not have email delivery configured."
)
"""The single response body of the request endpoint, in every case.

Read it carefully: it does not claim an email was SENT to you. It claims a
link was issued if the address is registered, and it names the real reason
one might never arrive. That wording is not hedging - it is the only
sentence that stays true across all five branches (unknown address,
inactive account, throttled account, NOT_CONFIGURED delivery, failed
send) while telling the caller nothing about which one they hit. Claiming
delivery unconditionally would be a fabrication in three of those five
(docs/TRADING_SAFETY.md's no-fabrication rule applied to notifications).
"""

INVALID_OR_EXPIRED_TOKEN = (
    "INVALID_OR_EXPIRED_TOKEN: this reset link is not valid. Reset links can be used "
    "once and expire; request a new one."
)

_TOO_MANY_REQUESTS = HTTPException(
    status_code=429,
    detail=(
        "Too many password reset requests from this address. Try again later."
    ),
)
"""429 is keyed on the CLIENT, never on the email, so it reveals nothing
about whether any particular address is registered - a caller sees it
identically whether they asked about a real account or a made-up one."""


def get_email_provider(request: Request) -> EmailProvider | None:
    """None means no email vendor is configured (D063) - callers must
    render that as NOT_CONFIGURED and route the link through the admin
    endpoint instead, never report a send that did not happen.

    Built once at startup in apps/api/app/main.py, like every other
    optional vendor in this app.
    """
    return request.app.state.email_provider


def _client_key(request: Request) -> str:
    """The throttle key. `request.client.host` and nothing else - see
    apps/api/app/auth/reset_throttle.py for why `X-Forwarded-For` is
    deliberately not consulted. A missing peer address (possible under some
    ASGI transports, including the in-process test client) collapses to one
    shared bucket rather than bypassing the throttle."""
    return request.client.host if request.client else "unknown"


@router.post("/auth/password-reset/request", response_model=PasswordResetAcknowledgement)
async def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    email_provider: EmailProvider | None = Depends(get_email_provider),
) -> PasswordResetAcknowledgement:
    now = password_reset.utcnow()

    if not reset_request_throttle.allow(
        _client_key(request),
        now,
        limit=settings.auth_password_reset_max_requests_per_ip_per_hour,
    ):
        raise _TOO_MANY_REQUESTS

    user = await reset_service.find_resettable_user(session, payload.email)
    if user is None:
        # No row is written for an unknown or inactive address, on purpose.
        # Writing one would make storage itself the oracle the response is
        # careful not to be, and would let anyone fill the table with rows
        # for addresses that can never redeem them.
        logger.info("password_reset_requested", outcome="no_resettable_account")
        return PasswordResetAcknowledgement(detail=ACKNOWLEDGEMENT)

    if await reset_service.account_throttle_exceeded(session, user.id, now, settings):
        logger.warning(
            "password_reset_throttled", user_id=str(user.id), scope="account"
        )
        return PasswordResetAcknowledgement(detail=ACKNOWLEDGEMENT)

    issued = await reset_service.issue_token(session, user, now, settings)

    if email_provider is None:
        # The token is real and redeemable; nothing was sent and nothing
        # pretends otherwise. An admin holding admin:manage can read the
        # link out of POST /admin/users/{id}/password-reset.
        logger.warning(
            "password_reset_email_not_sent",
            user_id=str(user.id),
            delivery="NOT_CONFIGURED",
        )
        return PasswordResetAcknowledgement(detail=ACKNOWLEDGEMENT)

    link = password_reset.build_reset_link(issued.raw_token, settings)
    try:
        await email_provider.send(
            to=user.email,
            subject=password_reset.RESET_EMAIL_SUBJECT,
            text=password_reset.build_reset_email_body(link, settings),
        )
    except EmailProviderError as exc:
        # The response cannot vary here without becoming an oracle, so the
        # structured log is where the truth lives. The token is left alive:
        # unlike the admin path, this caller has no other way to be handed
        # a link, and the provider may well have queued it before failing.
        logger.error(
            "password_reset_email_send_failed",
            user_id=str(user.id),
            error=str(exc),
        )
        return PasswordResetAcknowledgement(detail=ACKNOWLEDGEMENT)

    logger.info("password_reset_email_sent", user_id=str(user.id), delivery="SENT")
    return PasswordResetAcknowledgement(detail=ACKNOWLEDGEMENT)


@router.post("/auth/password-reset/confirm", response_model=PasswordResetAcknowledgement)
async def confirm_password_reset(
    payload: PasswordResetConfirmRequest,
    session: AsyncSession = Depends(get_session),
) -> PasswordResetAcknowledgement:
    now = password_reset.utcnow()
    user = await reset_service.redeem_token(
        session, payload.token, hash_password(payload.new_password), now
    )
    if user is None:
        # One sentinel for every failure mode. No stack trace, no hint at
        # which check failed, and deliberately no 404-vs-410 distinction
        # that would let a caller probe token validity.
        logger.warning("password_reset_confirm_rejected", reason="INVALID_OR_EXPIRED_TOKEN")
        raise HTTPException(status_code=400, detail=INVALID_OR_EXPIRED_TOKEN)

    logger.info("password_reset_confirmed", user_id=str(user.id))
    return PasswordResetAcknowledgement(
        detail="Password updated. Sign in with your new password."
    )
