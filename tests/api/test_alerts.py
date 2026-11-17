"""Alerts at the route and bot level (Phase 111, D138).

* `POST /admin/alerts/test` is admin-only, says NOT_CONFIGURED when there
  is no channel, and reports delivery only when the channel accepted.
* A library bot alerts once when it trades, stays silent when it has
  nothing to do, and says PAUSED when its kill switch fires - and a
  channel that is down changes nothing about the cycle.
"""

import contextlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.core.config import get_settings
from apps.api.app.db.base import get_session_factory
from apps.api.app.library.bot_runner import run_library_bot_cycle_all
from apps.api.app.main import app
from apps.api.app.notifications.alerts import AlertError, get_alert_sink
from tests.api.test_admin import _get_token, admin_user, api_client, db_session, non_admin_user
from tests.api.test_deployments import _h
from tests.api.test_library_bots import (
    NOW,
    Feed,
    feeds,
    new_active_bot,
    paper_broker,
    stop_off,
)
from tests.api.test_option_orders import options_user

D = Decimal


class RecordingSink:
    name = "recording"

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def send(self, text: str) -> None:
        if self.fail:
            raise AlertError("Telegram refused the message (HTTP 401): Unauthorized")
        self.sent.append(text)


@contextlib.contextmanager
def alert_sink(sink):
    app.dependency_overrides[get_alert_sink] = lambda: sink
    try:
        yield sink
    finally:
        app.dependency_overrides.pop(get_alert_sink, None)


@pytest.mark.asyncio
async def test_the_test_alert_is_admin_only():
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            with alert_sink(RecordingSink()) as sink:
                r = await client.post("/admin/alerts/test", headers=_h(token))
    assert r.status_code == 403
    assert sink.sent == []


@pytest.mark.asyncio
async def test_the_test_alert_says_not_configured_delivered_or_refused():
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)

            with alert_sink(None):
                r = await client.post("/admin/alerts/test", headers=_h(token))
            assert r.status_code == 503 and "NOT_CONFIGURED" in r.json()["detail"]

            with alert_sink(RecordingSink()) as sink:
                r = await client.post("/admin/alerts/test", headers=_h(token))
            assert r.status_code == 200, r.text
            assert r.json() == {
                "channel": "recording", "delivered": True, "detail": "accepted by the channel",
            }
            assert len(sink.sent) == 1 and "test alert" in sink.sent[0]

            with alert_sink(RecordingSink(fail=True)):
                r = await client.post("/admin/alerts/test", headers=_h(token))
            assert r.status_code == 502
            assert "ALERT_NOT_DELIVERED" in r.json()["detail"]
            assert "Unauthorized" in r.json()["detail"]


async def cycle(bot_id, feed, sink, at: datetime = NOW):
    return await run_library_bot_cycle_all(
        get_session_factory(),
        settings=get_settings(),
        provider=feed,
        quotes=feed,
        clock=lambda: at,
        only_bot_id=uuid.UUID(bot_id),
        alerts=sink,
    )


@pytest.mark.asyncio
async def test_a_bot_alerts_when_it_trades_and_when_it_is_paused_and_not_when_idle():
    feed = Feed(UP_US=0.002)
    sink = RecordingSink()
    async with (
        db_session() as session,
        options_user(session) as (uid, email),
        paper_broker(session, uid) as broker_id,
        api_client() as client,
        feeds(feed),
    ):
        await stop_off(session)
        token = await _get_token(client, email)
        bid = await new_active_bot(client, token, broker_id, max_drawdown_pct="10")

        assert (await cycle(bid, feed, sink)).outcomes[0].orders_filled == 1
        assert len(sink.sent) == 1
        assert sink.sent[0].startswith('Library bot "SPY trend" (paper): 1 filled, 0 rejected.')

        # Holding the target already: a completed cycle with nothing to say.
        assert (await cycle(bid, feed, sink)).outcomes[0].orders_filled == 0
        assert len(sink.sent) == 1

        price = D(str(round(feed.histories["UP.US"].closes[-1], 2)))
        feed.quote_price["UP.US"] = (price * D("0.8")).quantize(D("0.01"))
        await cycle(bid, feed, sink)
        assert len(sink.sent) == 2
        assert 'Library bot "SPY trend" (paper): PAUSED - Kill switch' in sink.sent[1]

        # Already paused: running it again is not news.
        await cycle(bid, feed, sink)
        assert len(sink.sent) == 2


@pytest.mark.asyncio
async def test_a_dead_alert_channel_changes_nothing_about_the_cycle():
    feed = Feed(UP_US=0.002)
    feed.clock = lambda: datetime.now(UTC)  # the route runs on the real clock
    async with (
        db_session() as session,
        options_user(session) as (uid, email),
        paper_broker(session, uid) as broker_id,
        api_client() as client,
        feeds(feed),
    ):
        await stop_off(session)
        token = await _get_token(client, email)
        bid = await new_active_bot(client, token, broker_id)
        with alert_sink(RecordingSink(fail=True)):
            r = await client.post(f"/library/bots/{bid}/run", headers=_h(token))
        assert r.status_code == 200, r.text
        assert r.json()["run_status"] == "succeeded" and r.json()["orders_filled"] == 1
