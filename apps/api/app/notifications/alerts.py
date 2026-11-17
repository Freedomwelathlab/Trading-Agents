"""Operator alerts over Telegram (Phase 111, D138).

A bot that trades while nobody is looking has to be able to say so. This
is the outbound channel: one short message to one chat when a bot trades,
is refused, is paused by its kill switch, or fails.

**Optional, and off until both values are set.** `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` are read whole or not at all; with either missing there
is no sink and every caller reports `NOT_CONFIGURED`.

**Delivery is claimed only when Telegram says `ok: true`** on a 200. A
timeout, an error status or an unreadable body raises `AlertError`.

**An alert can never break the thing it reports on.** Trading code calls
`notify`, which logs a failure and returns `False`; it does not raise. The
admin test endpoint calls `send` directly, because there the failure IS
the answer.

**The token is in the request URL** - that is how Telegram's Bot API
works - so no exception text, URL or response body from the HTTP client is
ever put into an error or a log line here. Only the exception's type name
and Telegram's own `description` field are.

**Outbound only.** Nothing here reads a message, and no instruction can
arrive through this channel.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

import httpx
from fastapi import Request

from apps.api.app.core.config import Settings
from apps.api.app.core.logging import get_logger

logger = get_logger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org"
MAX_TEXT = 4000
"""Telegram refuses a message over 4096 characters. Truncating is honest
here: the full record is the run row, the alert is a pointer to it."""


class AlertError(Exception):
    """The alert was not accepted. Never carries the token."""


class AlertSink(Protocol):
    name: str

    async def send(self, text: str) -> None:
        """Return normally only if the channel ACCEPTED the message."""
        ...


class TelegramAlertSink:
    name = "telegram"

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        base_url: str = TELEGRAM_API_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._transport = transport

    async def send(self, text: str) -> None:
        body = text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": body,
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0, transport=self._transport) as client:
                response = await client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            # The exception text carries the URL, and the URL carries the
            # token. The type name is all that is safe to repeat.
            raise AlertError(f"Telegram request failed: {type(exc).__name__}") from None
        try:
            answer = response.json()
        except ValueError:
            answer = None
        accepted = (
            response.status_code == 200 and isinstance(answer, dict) and answer.get("ok") is True
        )
        if not accepted:
            reason = answer.get("description") if isinstance(answer, dict) else None
            raise AlertError(
                f"Telegram refused the message (HTTP {response.status_code}): "
                f"{reason or 'no description'}"
            )


def build_alert_sink(settings: Settings) -> TelegramAlertSink | None:
    """None means no alert channel is configured; callers render that as
    NOT_CONFIGURED and never as a sent alert."""
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return None
    return TelegramAlertSink(
        bot_token=settings.telegram_bot_token, chat_id=settings.telegram_chat_id
    )


class AlertThrottle:
    """At most one alert per key per cooldown. In process memory only: a
    restart forgets it, which costs one repeated message and no state."""

    def __init__(self, *, cooldown_seconds: float = 3600.0, max_keys: int = 1024) -> None:
        self._cooldown = cooldown_seconds
        self._max_keys = max_keys
        self._last: dict[str, float] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        last = self._last.get(key)
        if last is not None and moment - last < self._cooldown:
            return False
        if len(self._last) >= self._max_keys:
            self._last.clear()
        self._last[key] = moment
        return True


FAILURE_THROTTLE = AlertThrottle()
"""A bot that fails every cycle fails every minute. It is reported once an
hour, not once a minute; the run rows hold every occurrence."""


def bot_cycle_alert_text(
    kind: str, name: str, *, opened: int, closed: int, failed: bool, detail: str | None
) -> str | None:
    """One line for a bot cycle that opened or closed a trade or failed;
    None for a cycle that did neither. A scan that found nothing is not
    news."""
    label = f'{kind} "{name}"'
    if failed:
        return f"{label}: cycle FAILED - {detail or 'no detail'}"
    if opened or closed:
        return f"{label}: {opened} opened, {closed} closed. {detail or ''}".rstrip()
    return None


async def alert_bot_cycle(
    sink: AlertSink | None,
    *,
    kind: str,
    name: str,
    key: str,
    opened: int,
    closed: int,
    failed: bool,
    detail: str | None,
    throttle: AlertThrottle = FAILURE_THROTTLE,
) -> bool:
    """Best-effort alert for one bot cycle. Never raises. Trades are always
    sent; a failure is sent at most once per cooldown per bot."""
    if sink is None:
        return False
    text = bot_cycle_alert_text(
        kind, name, opened=opened, closed=closed, failed=failed, detail=detail
    )
    if text is None:
        return False
    if failed and not throttle.allow(f"{kind}:{key}"):
        return False
    return await notify(sink, text)


def get_alert_sink(request: Request) -> AlertSink | None:
    """The sink built once at startup, or None (NOT_CONFIGURED)."""
    sink: AlertSink | None = getattr(request.app.state, "alert_sink", None)
    return sink


async def notify(sink: AlertSink | None, text: str) -> bool:
    """Best-effort send for trading code. True only if the channel
    accepted the message; never raises."""
    if sink is None:
        return False
    try:
        await sink.send(text)
    except AlertError as exc:
        logger.warning("alert_not_delivered", channel=sink.name, reason=str(exc))
        return False
    except Exception as exc:  # noqa: BLE001 - an alert must not break its caller
        logger.warning("alert_not_delivered", channel=sink.name, reason=type(exc).__name__)
        return False
    return True
