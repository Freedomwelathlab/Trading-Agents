"""Concrete EmailProvider talking to a generic transactional-email HTTP
API. Resend is the shape this was written against; nothing here is
Resend-specific, for the same reason the LLM client in
apps/api/app/agents/anthropic_compatible.py stays provider-name-agnostic
(docs/DECISIONS.md D018, D062).

THE EXACT CONTRACT, so a different vendor can be judged against it without
reading the code:

    POST {EMAIL_PROVIDER_BASE_URL}/emails
    Authorization: Bearer {EMAIL_PROVIDER_API_KEY}
    Content-Type: application/json

    {"from": "{EMAIL_PROVIDER_FROM_ADDRESS}",
     "to": ["recipient@example.com"],
     "subject": "...",
     "text": "..."}

    -> any 2xx  = ACCEPTED for delivery
    -> anything else, or a transport failure = EmailProviderError

`to` is a LIST even for a single recipient, because that is what Resend
and most of its lookalikes require and a bare string is silently
misinterpreted by some of them. The body is plain text only - no HTML
part. A reset email is one sentence and one URL; an HTML template would
add a rendering surface, a second place for the link to diverge from the
text part, and nothing a user benefits from.

"Accepted" is the strongest claim this client will ever make, and it is
the honest one: a 2xx from a transactional-email API means the vendor
queued the message, not that it reached an inbox. Nothing in this codebase
upgrades that to "delivered".
"""

from typing import Any

import httpx

from apps.api.app.core.config import Settings
from apps.api.app.notifications.provider import EmailProviderError


class HttpTransactionalEmailProvider:
    name = "http-transactional-email"

    def __init__(self, *, base_url: str, api_key: str, from_address: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._from_address = from_address

    async def send(self, *, to: str, subject: str, text: str) -> None:
        payload: dict[str, Any] = {
            "from": self._from_address,
            "to": [to],
            "subject": subject,
            "text": text,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{self._base_url}/emails", json=payload, headers=headers
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            # The exception text can carry the request URL but never the
            # bearer token or the body, so this is safe to propagate; the
            # callers that log it go through the redacting processor in
            # apps/api/app/core/logging.py regardless.
            raise EmailProviderError(f"Email provider request failed: {exc}") from exc


def build_email_provider(settings: Settings) -> HttpTransactionalEmailProvider | None:
    """None means no provider is configured (D062) - callers must render
    that as NOT_CONFIGURED and hand the reset link over some other audited
    channel, never silently skip the send and report success."""
    if not (
        settings.email_provider_base_url
        and settings.email_provider_api_key
        and settings.email_provider_from_address
    ):
        return None
    return HttpTransactionalEmailProvider(
        base_url=settings.email_provider_base_url,
        api_key=settings.email_provider_api_key,
        from_address=settings.email_provider_from_address,
    )
