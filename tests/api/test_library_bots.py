"""Strategy Library paper bots (Phase 109, D136): the human gate over HTTP,
and full cycles against the real database and the real order path with a
stubbed history-and-quote vendor and an injected clock.

What these pin down: a bot trades only after approval and only on paper;
it buys what the strategy holds and sells what it no longer holds; it will
not trade on a stale quote; it sizes inside the Risk Engine's position cap
instead of being rejected by it; its kill switch sells and pauses; and
every cycle leaves a run row that says what happened."""

import contextlib
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from apps.api.app.api.routes.library_bots import get_bot_feeds
from apps.api.app.core.config import get_settings
from apps.api.app.db.base import get_session_factory
from apps.api.app.db.models import (
    BrokerKind,
    Fill,
    LibraryBot,
    LibraryBotRun,
    LibraryBotRunStatus,
    LibraryBotStatus,
    Order,
)
from apps.api.app.library import service as library_service
from apps.api.app.library.bot import quantize
from apps.api.app.library.bot_runner import run_library_bot_cycle_all
from apps.api.app.library.engine import PriceTable
from apps.api.app.library.quality import completed_sessions, market_today
from apps.api.app.main import app
from apps.api.app.marketdata.models import MarketSnapshot
from apps.api.app.marketdata.provider import DataUnavailableError
from apps.api.app.marketdata.providers.yahoo import DailyHistory
from apps.api.app.safety.emergency_stop import set_emergency_stop
from tests.api.test_admin import _get_token, api_client, db_session
from tests.api.test_deployments import _h, deploy_only_user
from tests.api.test_option_orders import granted_broker, options_user

D = Decimal
NOW = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)  # a Tuesday, 10:00 New York


def weekdays_ending(last: date, n: int) -> tuple[date, ...]:
    out, d = [], last
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return tuple(reversed(out))


def series(symbol: str, daily: float, n: int = 320, start: float = 100.0) -> DailyHistory:
    """`n` completed sessions ending the Monday before NOW."""
    closes = tuple(start * (1 + daily) ** k for k in range(n))
    return DailyHistory(
        symbol, symbol, "USD", weekdays_ending(date(2026, 11, 16), n), closes, closes
    )


class Feed:
    """History and quotes from one place, both controllable per test."""

    name = "fake"

    def __init__(self, **daily: float) -> None:
        self.histories = {
            s.replace("_", "."): series(s.replace("_", "."), r) for s, r in daily.items()
        }
        self.quote_age = timedelta(seconds=30)
        self.quote_price: dict[str, Decimal] = {}
        self.quoted: list[str] = []
        self.clock = lambda: NOW

    async def get_daily_history(self, symbol, *, start, end):
        if symbol not in self.histories:
            raise DataUnavailableError(f"no such symbol {symbol}")
        return self.histories[symbol]

    async def get_quote(self, symbol):
        self.quoted.append(symbol)
        if symbol not in self.histories:
            raise DataUnavailableError(f"no quote for {symbol}")
        price = self.quote_price.get(symbol) or D(str(round(self.histories[symbol].closes[-1], 2)))
        return MarketSnapshot(
            symbol=symbol, price=price, as_of=self.clock() - self.quote_age, source="fake"
        )


@contextlib.asynccontextmanager
async def feeds(feed: Feed | None):
    library_service.reset_caches()
    app.dependency_overrides[get_bot_feeds] = lambda: (feed, feed)
    try:
        yield feed
    finally:
        app.dependency_overrides.pop(get_bot_feeds, None)
        library_service.reset_caches()


@contextlib.asynccontextmanager
async def paper_broker(session, user_id, *, kind=BrokerKind.PAPER):
    """`granted_broker`, plus the equity orders and bots these tests create
    (its own teardown only knows about option rows)."""
    async with granted_broker(session, user_id, kind=kind) as broker_id:
        try:
            yield broker_id
        finally:
            await session.rollback()
            bot_ids = (
                (
                    await session.execute(
                        select(LibraryBot.id).where(LibraryBot.broker_id == broker_id)
                    )
                )
                .scalars()
                .all()
            )
            for bid in bot_ids:
                await session.execute(delete(LibraryBotRun).where(LibraryBotRun.bot_id == bid))
            await session.execute(delete(LibraryBot).where(LibraryBot.broker_id == broker_id))
            order_ids = select(Order.id).where(Order.broker_id == broker_id)
            await session.execute(delete(Fill).where(Fill.order_id.in_(order_ids)))
            await session.execute(delete(Order).where(Order.broker_id == broker_id))
            await session.commit()


def body(broker_id, **over):
    base = dict(
        name="SPY trend",
        broker_id=str(broker_id),
        strategy_id="trend_200d",
        symbols=["up.us"],
        capital="5000",
    )
    base.update(over)
    return base


async def stop_off(session) -> None:
    await set_emergency_stop(session, active=False, reason="library bot test", actor_user_id=None)
    await session.commit()


async def cycle(bot_id, feed, at: datetime = NOW):
    return await run_library_bot_cycle_all(
        get_session_factory(),
        settings=get_settings(),
        provider=feed,
        quotes=feed,
        clock=lambda: at,
        only_bot_id=bot_id,
    )


async def new_active_bot(client, token, broker_id, **over) -> str:
    r = await client.post("/library/bots", json=body(broker_id, **over), headers=_h(token))
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    r = await client.post(f"/library/bots/{bid}/approve", headers=_h(token))
    assert r.status_code == 200 and r.json()["status"] == "active", r.text
    return bid


async def fetch(session, bot_id) -> LibraryBot:
    session.expire_all()
    bot = await session.get(LibraryBot, uuid.UUID(bot_id))
    assert bot is not None
    return bot


# ------------------------------------------------------------ pure pieces


def test_quantities_are_whole_except_crypto() -> None:
    assert quantize("SPY.US", D("27.9")) == D(27)
    assert quantize("BTC-USD", D("0.123456789")) == D("0.12345678")
    assert quantize("D05.SG", D("-3")) == D(0)


def test_the_session_in_progress_is_not_a_close() -> None:
    days = (date(2026, 11, 13), date(2026, 11, 16), date(2026, 11, 17))
    h = DailyHistory("SPY.US", "SPY", "USD", days, (1.0, 2.0, 3.0), (1.0, 2.0, 3.0))
    assert market_today("SPY.US", NOW) == date(2026, 11, 17)
    assert completed_sessions(h, NOW).dates == days[:2]
    # 15:00 UTC on the 17th is already 23:00 in Singapore - still the 17th.
    assert market_today("D05.SG", NOW) == date(2026, 11, 17)
    # 01:00 UTC on the 18th is the 17th in New York but the 18th in Singapore.
    late = datetime(2026, 11, 18, 1, 0, tzinfo=UTC)
    assert market_today("SPY.US", late) == date(2026, 11, 17)
    assert market_today("D05.SG", late) == date(2026, 11, 18)
    assert market_today("BTC-USD", late) == date(2026, 11, 18)
    done = DailyHistory("SPY.US", "SPY", "USD", days[:2], (1.0, 2.0), (1.0, 2.0))
    assert completed_sessions(done, NOW) is done


def test_the_last_row_can_be_a_month_end() -> None:
    def table(last: date, n: int = 5) -> PriceTable:
        days = weekdays_ending(last, n)
        return PriceTable([DailyHistory("A.US", "A", "USD", days, (1.0,) * n, (1.0,) * n)])

    assert table(date(2026, 10, 30)).is_month_end(4)  # Friday; Monday is 2 November
    assert not table(date(2026, 11, 16)).is_month_end(4)
    daily = tuple(date(2026, 10, 27) + timedelta(days=k) for k in range(5))  # to Sat 31 Oct
    crypto = PriceTable([DailyHistory("BTC-USD", "BTC-USD", "USD", daily, (1.0,) * 5, (1.0,) * 5)])
    assert crypto.is_month_end(4) and not crypto.is_month_end(3)


# ------------------------------------------------------------ the human gate


@pytest.mark.asyncio
async def test_creating_needs_deploy_and_approving_needs_its_own_permission():
    async with (
        db_session() as session,
        deploy_only_user(session) as (uid, email),
        paper_broker(session, uid) as broker_id,
        api_client() as client,
        feeds(Feed(UP_US=0.002)),
    ):
        token = await _get_token(client, email)
        r = await client.post("/library/bots", json=body(broker_id), headers=_h(token))
        assert r.status_code == 201, r.text
        bot = r.json()
        assert bot["status"] == "pending_approval" and bot["symbols"] == ["UP.US"]
        assert bot["cash"] == bot["capital"] and bot["positions"] == {}
        assert bot["return_pct"] is None and bot["target_weights"] is None
        assert bot["params"] == {"sma_days": 200}
        r = await client.post(f"/library/bots/{bot['id']}/approve", headers=_h(token))
        assert r.status_code == 403
        # Unapproved: "run now" records why it did nothing and places no order.
        r = await client.post(f"/library/bots/{bot['id']}/run", headers=_h(token))
        assert r.status_code == 200 and r.json()["run_status"] == "skipped_not_active"
        assert r.json()["orders_filled"] == 0


@pytest.mark.asyncio
async def test_guardrails_at_creation():
    async with (
        db_session() as session,
        options_user(session) as (uid, email),
        paper_broker(session, uid) as broker_id,
        api_client() as client,
    ):
        token = await _get_token(client, email)

        async def post(**over):
            return await client.post(
                "/library/bots", json=body(broker_id, **over), headers=_h(token)
            )

        assert "BAD_STRATEGY" in (await post(strategy_id="nope")).json()["detail"]
        assert "BAD_STRATEGY" in (await post(strategy_id="buy_and_hold")).json()["detail"]
        assert "BAD_PARAMS" in (await post(params={"sma_days": 1})).json()["detail"]
        assert "BAD_SYMBOLS" in (await post(symbols=["AAPL"])).json()["detail"]
        assert "BAD_SYMBOLS" in (await post(strategy_id="dual_momentum")).json()["detail"]
        assert "BAD_CAPITAL" in (await post(capital="5")).json()["detail"]
        assert "BAD_LIMITS" in (await post(max_drawdown_pct="95")).json()["detail"]
        r = await client.post("/library/bots", json=body(uuid.uuid4()), headers=_h(token))
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_a_bot_cannot_be_created_on_a_live_broker():
    async with (
        db_session() as session,
        options_user(session) as (uid, email),
        paper_broker(session, uid, kind=BrokerKind.LIVE) as broker_id,
        api_client() as client,
    ):
        token = await _get_token(client, email)
        r = await client.post("/library/bots", json=body(broker_id), headers=_h(token))
        assert r.status_code == 400 and "LIVE_NOT_SUPPORTED" in r.json()["detail"]


# ------------------------------------------------------------ cycles


@pytest.mark.asyncio
async def test_a_bot_buys_what_the_rule_holds_then_sits_still():
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

        r = await client.post(f"/library/bots/{bid}/run", headers=_h(token))
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["run_status"] == "succeeded" and out["orders_filled"] == 1, out
        bot = (await client.get(f"/library/bots/{bid}", headers=_h(token))).json()
        price = feed.histories["UP.US"].closes[-1]
        held = D(bot["positions"]["UP.US"])
        assert held == D(int(5000 / round(price, 2)))
        assert D(bot["cash"]) < D("200") and D(bot["cash"]) >= 0
        assert bot["target_weights"] == {"UP.US": 1.0}
        assert "above" in bot["target_explain"]["UP.US"]
        assert bot["last_decision_date"] is not None
        assert abs(D(bot["return_pct"])) < D("1")

        runs = (await client.get(f"/library/bots/{bid}/runs", headers=_h(token))).json()["items"]
        order = runs[0]["orders"][0]
        assert order["side"] == "buy" and order["outcome"] == "filled"
        assert order["quote_source"] == "fake" and order["order_id"]
        rows = (
            (await session.execute(select(Order).where(Order.broker_id == broker_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1 and rows[0].symbol == "UP.US"

        # Same decision, holdings already match: nothing is sent.
        again = (await client.post(f"/library/bots/{bid}/run", headers=_h(token))).json()
        assert again["orders_filled"] == 0 and "holding the target already" in again["run_detail"]
        rows = (
            (await session.execute(select(Order).where(Order.broker_id == broker_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_a_bot_sells_when_the_rule_goes_to_cash():
    feed = Feed(UP_US=0.002)
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
        assert (await cycle(uuid.UUID(bid), feed)).outcomes[0].orders_filled == 1

        # The same symbol is now far below its average: the rule holds cash.
        library_service.reset_caches()
        last = feed.histories["UP.US"].closes[-1]
        falling = series("UP.US", -0.004, start=last * 4)
        feed.histories["UP.US"] = falling
        result = await cycle(uuid.UUID(bid), feed)
        outcome = result.outcomes[0]
        assert outcome.status is LibraryBotRunStatus.SUCCEEDED and outcome.orders_filled == 1
        bot = await fetch(session, bid)
        assert bot.positions == {} and bot.target["weights"] == {}
        assert bot.cash > 0 and bot.last_equity == bot.cash


@pytest.mark.asyncio
async def test_a_stale_quote_is_not_traded_and_is_retried():
    feed = Feed(UP_US=0.002)
    feed.quote_age = timedelta(hours=3)
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
        outcome = (await cycle(uuid.UUID(bid), feed)).outcomes[0]
        assert outcome.status is LibraryBotRunStatus.SUCCEEDED and outcome.orders_filled == 0
        bot = await fetch(session, bid)
        assert bot.positions == {} and bot.last_decision_date is None
        run = (
            (await session.execute(select(LibraryBotRun).where(LibraryBotRun.bot_id == bot.id)))
            .scalars()
            .one()
        )
        assert (
            run.orders[0]["outcome"] == "skipped" and "180 minutes old" in run.orders[0]["detail"]
        )
        orders = (
            (await session.execute(select(Order).where(Order.broker_id == broker_id)))
            .scalars()
            .all()
        )
        assert orders == []

        feed.quote_age = timedelta(seconds=20)
        outcome = (await cycle(uuid.UUID(bid), feed)).outcomes[0]
        assert outcome.orders_filled == 1


@pytest.mark.asyncio
async def test_an_order_is_sized_under_the_position_cap_not_bounced_off_it():
    feed = Feed(UP_US=0.002)
    async with (
        db_session() as session,
        options_user(session) as (uid, email),
        paper_broker(session, uid) as broker_id,
        api_client() as client,
        feeds(feed),
    ):
        await stop_off(session)
        token = await _get_token(client, email)
        # 60,000 of a 100,000 paper account in one symbol: the Risk Engine
        # allows 10% of the account per position.
        bid = await new_active_bot(client, token, broker_id, capital="60000")
        outcome = (await cycle(uuid.UUID(bid), feed)).outcomes[0]
        assert outcome.orders_filled == 1 and outcome.orders_rejected == 0
        bot = await fetch(session, bid)
        price = D(str(round(feed.histories["UP.US"].closes[-1], 2)))
        value = D(bot.positions["UP.US"]) * price
        assert D("9000") < value <= D("10000")
        run = (
            (await session.execute(select(LibraryBotRun).where(LibraryBotRun.bot_id == bot.id)))
            .scalars()
            .one()
        )
        assert "position cap" in run.orders[0]["detail"]
        # Holding less than the target is not a reason to hammer the cap.
        outcome = (await cycle(uuid.UUID(bid), feed)).outcomes[0]
        assert outcome.orders_filled == 0 and outcome.orders_rejected == 0


@pytest.mark.asyncio
async def test_the_kill_switch_sells_and_pauses():
    feed = Feed(UP_US=0.002)
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
        assert (await cycle(uuid.UUID(bid), feed)).outcomes[0].orders_filled == 1

        price = D(str(round(feed.histories["UP.US"].closes[-1], 2)))
        feed.quote_price["UP.US"] = (price * D("0.8")).quantize(D("0.01"))
        outcome = (await cycle(uuid.UUID(bid), feed)).outcomes[0]
        assert outcome.orders_filled == 1 and "kill switch" in outcome.detail
        bot = await fetch(session, bid)
        assert bot.status is LibraryBotStatus.PAUSED and "Kill switch" in bot.paused_reason
        assert bot.positions == {}
        # Paused: the next cycle does nothing.
        outcome = (await cycle(uuid.UUID(bid), feed)).outcomes[0]
        assert outcome.status is LibraryBotRunStatus.SKIPPED_NOT_ACTIVE
        # Resuming starts a fresh peak, so it does not trip again at once.
        r = await client.post(f"/library/bots/{bid}/resume", headers=_h(token))
        assert r.json()["status"] == "active"
        outcome = (await cycle(uuid.UUID(bid), feed)).outcomes[0]
        assert "kill switch" not in (outcome.detail or "")


@pytest.mark.asyncio
async def test_emergency_stop_and_a_switched_off_feed_place_nothing():
    feed = Feed(UP_US=0.002)
    async with (
        db_session() as session,
        options_user(session) as (uid, email),
        paper_broker(session, uid) as broker_id,
        api_client() as client,
        feeds(feed),
    ):
        token = await _get_token(client, email)
        bid = await new_active_bot(client, token, broker_id)
        await set_emergency_stop(session, active=True, reason="test", actor_user_id=None)
        await session.commit()
        try:
            outcome = (await cycle(uuid.UUID(bid), feed)).outcomes[0]
            assert outcome.status is LibraryBotRunStatus.SKIPPED_EMERGENCY_STOP
        finally:
            await stop_off(session)
        outcome = (await cycle(uuid.UUID(bid), None)).outcomes[0]
        assert outcome.status is LibraryBotRunStatus.SKIPPED_NOT_CONFIGURED
        assert "NOT_CONFIGURED" in outcome.detail
        orders = (
            (await session.execute(select(Order).where(Order.broker_id == broker_id)))
            .scalars()
            .all()
        )
        assert orders == [] and feed.quoted == []


@pytest.mark.asyncio
async def test_a_missing_symbol_fails_the_cycle_with_the_reason():
    feed = Feed(UP_US=0.002)
    async with (
        db_session() as session,
        options_user(session) as (uid, email),
        paper_broker(session, uid) as broker_id,
        api_client() as client,
        feeds(feed),
    ):
        await stop_off(session)
        token = await _get_token(client, email)
        bid = await new_active_bot(client, token, broker_id, symbols=["GONE.US"])
        outcome = (await cycle(uuid.UUID(bid), feed)).outcomes[0]
        assert outcome.status is LibraryBotRunStatus.FAILED
        assert "DATA_UNAVAILABLE" in outcome.detail


@pytest.mark.asyncio
async def test_a_short_weight_is_left_as_cash():
    feed = Feed(DOWN_US=-0.002)
    async with (
        db_session() as session,
        options_user(session) as (uid, email),
        paper_broker(session, uid) as broker_id,
        api_client() as client,
        feeds(feed),
    ):
        await stop_off(session)
        token = await _get_token(client, email)
        bid = await new_active_bot(
            client,
            token,
            broker_id,
            strategy_id="tsmom_12m",
            symbols=["DOWN.US"],
            params={"allow_short": 1},
        )
        outcome = (await cycle(uuid.UUID(bid), feed)).outcomes[0]
        assert outcome.orders_filled == 0 and "long-only" in outcome.detail
        assert (await fetch(session, bid)).positions == {}


@pytest.mark.asyncio
async def test_delete_is_refused_while_running_or_holding():
    feed = Feed(UP_US=0.002)
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
        r = await client.post(f"/library/bots/{bid}/delete", headers=_h(token))
        assert r.status_code == 409 and "BOT_RUNNING" in r.json()["detail"]
        assert (await cycle(uuid.UUID(bid), feed)).outcomes[0].orders_filled == 1
        await client.post(f"/library/bots/{bid}/stop", headers=_h(token))
        r = await client.post(f"/library/bots/{bid}/delete", headers=_h(token))
        assert r.status_code == 409 and "BOT_HOLDS_POSITIONS" in r.json()["detail"]

        flat = (await client.post("/library/bots", json=body(broker_id), headers=_h(token))).json()
        r = await client.post(f"/library/bots/{flat['id']}/delete", headers=_h(token))
        assert r.status_code == 200 and r.json()["deleted"] is True
        r = await client.get(f"/library/bots/{flat['id']}", headers=_h(token))
        assert r.status_code == 404
        listed = (await client.get("/library/bots", headers=_h(token))).json()["items"]
        assert [b["id"] for b in listed] == [bid]
