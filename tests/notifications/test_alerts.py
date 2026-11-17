"""Operator alerts over Telegram (Phase 111, D138).

The HTTP layer runs for real against `httpx.MockTransport`; nothing
reaches the network and no real bot token exists in this file.

What is asserted:

* Delivery is claimed only on HTTP 200 with `ok: true`.
* The bot token - which is part of the request URL - never appears in an
  error, whatever went wrong.
* `notify` never raises: an alert cannot break the cycle it reports on.
* No channel is built from half a configuration.
* An idle cycle is silent; a trade, a refusal, a kill-switch pause and a
  failure each produce one line.
"""

import json
import uuid
from decimal import Decimal

import httpx
import pytest

from apps.api.app.core.config import Settings
from apps.api.app.db.models import LibraryBotRunStatus
from apps.api.app.library.bot import LibraryBotOutcome
from apps.api.app.library.bot_runner import library_bot_alert_text
from apps.api.app.notifications.alerts import (
    MAX_TEXT,
    AlertError,
    AlertThrottle,
    TelegramAlertSink,
    alert_bot_cycle,
    bot_cycle_alert_text,
    build_alert_sink,
    notify,
)

TOKEN = "123456:fake-telegram-token-for-tests"  # pragma: allowlist secret


def sink_with(handler) -> TelegramAlertSink:
    return TelegramAlertSink(
        bot_token=TOKEN, chat_id="-100200300", transport=httpx.MockTransport(handler)
    )


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256",
        **overrides,
    )


@pytest.mark.asyncio
async def test_a_message_is_posted_to_the_chat_and_accepted_on_ok_true():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    await sink_with(handler).send("SPY trend: 1 filled")
    assert seen["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert seen["body"] == {
        "chat_id": "-100200300",
        "text": "SPY trend: 1 filled",
        "disable_web_page_preview": True,
    }


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"ok": False, "description": "Unauthorized"}, "HTTP 401.*Unauthorized"),
        (400, {"ok": False, "description": "Bad Request: chat not found"}, "chat not found"),
        (200, {"ok": False, "description": "odd"}, "HTTP 200.*odd"),
        (200, {"result": {}}, "HTTP 200.*no description"),
        (502, None, "HTTP 502.*no description"),
    ],
)
@pytest.mark.asyncio
async def test_anything_but_200_with_ok_true_is_not_a_delivery(status, body, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status, text="<html>bad gateway</html>")
        return httpx.Response(status, json=body)

    with pytest.raises(AlertError, match=expected) as caught:
        await sink_with(handler).send("x")
    assert TOKEN not in str(caught.value)


@pytest.mark.asyncio
async def test_a_network_failure_never_leaks_the_token_in_the_url():
    def handler(request: httpx.Request) -> httpx.Response:
        # httpx puts the request URL - token included - in this message.
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(AlertError) as caught:
        await sink_with(handler).send("x")
    assert str(caught.value) == "Telegram request failed: ConnectError"
    assert caught.value.__cause__ is None  # the chained error would carry the URL


@pytest.mark.asyncio
async def test_an_overlong_message_is_cut_to_what_telegram_accepts():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["text"] = json.loads(request.content)["text"]
        return httpx.Response(200, json={"ok": True})

    await sink_with(handler).send("a" * 9000)
    assert len(seen["text"]) == MAX_TEXT and seen["text"].endswith("…")


@pytest.mark.asyncio
async def test_notify_reports_the_outcome_and_never_raises():
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    def accept(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    class Broken:
        name = "broken"

        async def send(self, text: str) -> None:
            raise RuntimeError("something unrelated")

    assert await notify(None, "x") is False
    assert await notify(sink_with(refuse), "x") is False
    assert await notify(Broken(), "x") is False
    assert await notify(sink_with(accept), "x") is True


def test_no_channel_is_built_from_half_a_configuration():
    assert build_alert_sink(_settings()) is None
    assert build_alert_sink(_settings(telegram_bot_token=TOKEN)) is None
    assert build_alert_sink(_settings(telegram_chat_id="1")) is None
    sink = build_alert_sink(_settings(telegram_bot_token=TOKEN, telegram_chat_id="1"))
    assert sink is not None and sink.name == "telegram"


def test_no_alert_channel_is_configured_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    settings = _settings()
    assert settings.telegram_bot_token is None and settings.telegram_chat_id is None


# --- what a library bot says --------------------------------------------------


def outcome(status=LibraryBotRunStatus.SUCCEEDED, **over) -> LibraryBotOutcome:
    return LibraryBotOutcome(bot_id=uuid.uuid4(), run_id=uuid.uuid4(), status=status, **over)


def test_an_idle_cycle_is_silent():
    assert library_bot_alert_text("SPY trend", outcome(detail="nothing to do"), None) is None


def test_a_trade_a_refusal_a_pause_and_a_failure_each_say_so():
    traded = library_bot_alert_text(
        "SPY trend",
        outcome(orders_filled=1, equity=Decimal("8012.4"), detail="1 filled, 0 rejected"),
        None,
    )
    assert traded == (
        'Library bot "SPY trend" (paper): 1 filled, 0 rejected. Equity 8,012.40. '
        "1 filled, 0 rejected"
    )
    refused = library_bot_alert_text("SPY trend", outcome(orders_rejected=2), None)
    assert refused == 'Library bot "SPY trend" (paper): 0 filled, 2 rejected.'

    paused = library_bot_alert_text(
        "SPY trend", outcome(orders_filled=1), "Kill switch: drawdown limit of 10% reached"
    )
    assert paused == (
        'Library bot "SPY trend" (paper): PAUSED - '
        "Kill switch: drawdown limit of 10% reached"
    )

    failed = library_bot_alert_text(
        "SPY trend", outcome(LibraryBotRunStatus.FAILED, detail="DataUnavailableError: x"), None
    )
    assert failed == 'Library bot "SPY trend" (paper): cycle FAILED - DataUnavailableError: x'


# --- the autotrade and options bots -------------------------------------------


def test_a_bot_cycle_that_neither_traded_nor_failed_is_silent():
    assert (
        bot_cycle_alert_text(
            "Autotrade bot", "BTC 24h", opened=0, closed=0, failed=False, detail="no signal"
        )
        is None
    )


def test_a_bot_cycle_says_what_it_opened_closed_or_why_it_failed():
    traded = bot_cycle_alert_text(
        "Options bot", "SPY puts", opened=1, closed=2, failed=False, detail="opened bull put"
    )
    assert traded == 'Options bot "SPY puts": 1 opened, 2 closed. opened bull put'
    bare = bot_cycle_alert_text(
        "Autotrade bot", "BTC 24h", opened=0, closed=1, failed=False, detail=None
    )
    assert bare == 'Autotrade bot "BTC 24h": 0 opened, 1 closed.'
    failed = bot_cycle_alert_text(
        "Autotrade bot", "BTC 24h", opened=0, closed=0, failed=True, detail="VendorError: 503"
    )
    assert failed == 'Autotrade bot "BTC 24h": cycle FAILED - VendorError: 503'


def test_the_throttle_allows_one_per_key_per_cooldown():
    throttle = AlertThrottle(cooldown_seconds=3600, max_keys=2)
    assert throttle.allow("a", now=0) is True
    assert throttle.allow("a", now=3599) is False
    assert throttle.allow("b", now=10) is True
    assert throttle.allow("a", now=3600) is True
    # Over the key cap it forgets rather than grows: one repeat, no leak.
    assert throttle.allow("c", now=3601) is True
    assert throttle.allow("b", now=3602) is True


class Recorder:
    name = "recorder"

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


@pytest.mark.asyncio
async def test_a_bot_failing_every_cycle_is_reported_once_but_every_trade_is_sent():
    sink, throttle = Recorder(), AlertThrottle(cooldown_seconds=3600)

    async def cycle(**over) -> bool:
        base = dict(kind="Autotrade bot", name="BTC 24h", key="bot-1", opened=0, closed=0,
                    failed=False, detail="x", throttle=throttle)
        return await alert_bot_cycle(sink, **{**base, **over})

    assert await cycle(failed=True) is True
    assert await cycle(failed=True) is False  # same bot, within the hour
    assert await cycle(failed=True, key="bot-2") is True  # a different bot
    assert await cycle(opened=1) is True
    assert await cycle(opened=1) is True  # trades are never throttled
    assert await cycle() is False  # nothing happened
    assert len(sink.sent) == 4

    assert await alert_bot_cycle(
        None, kind="k", name="n", key="x", opened=1, closed=0, failed=False, detail=None
    ) is False
