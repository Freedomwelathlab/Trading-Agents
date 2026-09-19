"""Bot engine, end to end against the real database and the real OMS
(Phase 81, D098).

The vendor is a stub that serves synthetic bars, and the setup registry is
patched with one detector that always fires LONG at the last bar — so the
test exercises the ENGINE (limits, sizing, the order path, the ledger, the
exits) without depending on any real pattern's geometry. The real
detectors are measured separately (D095) and by `test_scanner.py`.

Every order this test places is a real `orders` row through
`submit_trade_and_record`; nothing is mocked below the engine.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

import apps.api.app.autotrade.scanner as scanner_module
from apps.api.app.autotrade.engine import run_bot_cycle
from apps.api.app.autotrade.service import BotSpec, approve_bot, create_bot
from apps.api.app.backtesting.setups import SetupSignal
from apps.api.app.core.config import get_settings
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    AutotradeBot,
    AutotradeBotRun,
    AutotradeBotRunStatus,
    AutotradeBotTrade,
    Broker,
    BrokerAccount,
    BrokerGrant,
    BrokerKind,
    BrokerPosition,
    Fill,
    MarketDataBar,
    Order,
    Role,
    User,
)
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.structure import Direction

SYMBOL = "ZZTEST.US"
SESSION = date(2026, 6, 17)
# 09:30 ET on 2026-06-17 is 13:30 UTC (EDT).
OPEN_UTC = datetime(2026, 6, 17, 13, 30, tzinfo=UTC)


@contextlib.asynccontextmanager
async def db_session():
    gen = get_session()
    session = await anext(gen)
    try:
        yield session
    finally:
        await gen.aclose()


def bars(prices: list[Decimal], *, start: datetime = OPEN_UTC) -> list[Bar]:
    """One 5-minute bar per price; high/low a little either side."""
    out = []
    for i, p in enumerate(prices):
        out.append(
            Bar(
                symbol=SYMBOL, bar_interval="5m", ts=start + timedelta(minutes=5 * i),
                open=p, high=p + Decimal("0.2"), low=p - Decimal("0.2"), close=p,
                volume=1000, source="stub",
            )
        )
    return out


class StubRouter:
    """Serves whatever bars the test hands it; records the calls."""

    configured_vendors = ("longbridge", "coinbase")

    def __init__(self) -> None:
        self.bars: list[Bar] = []
        self.calls = 0

    async def get_bars(self, symbol, *, bar_interval, start_date, end_date):
        self.calls += 1
        return [b for b in self.bars if b.symbol == symbol]


def always_long(ctx) -> SetupSignal | None:
    bar = ctx.bar
    return SetupSignal(
        setup_name="stub_long", direction=Direction.LONG, entry_index=ctx.index,
        entry_price=bar.close, stop_price=bar.close * Decimal("0.98"), score=5,
        evidence={"why": "stub"},
    )


@contextlib.asynccontextmanager
async def fixture(session):
    tag = uuid.uuid4().hex[:8]
    role = Role(id=uuid.uuid4(), name=f"t-bot-{tag}", permissions=[])
    user = User(id=uuid.uuid4(), email=f"bot-{tag}@example.com",
                hashed_password="x", is_active=True, role_id=role.id)
    broker = Broker(id=uuid.uuid4(), name=f"Bot paper {tag}", kind=BrokerKind.PAPER,
                    provider="paper", is_active=True)
    session.add_all([role, user, broker])
    await session.flush()
    session.add(BrokerGrant(id=uuid.uuid4(), user_id=user.id, broker_id=broker.id))
    await session.commit()
    ids = (user.id, broker.id, role.id)
    try:
        yield ids
    finally:
        await session.rollback()
        user_id, broker_id, role_id = ids
        bot_ids = list(
            (
                await session.execute(
                    select(AutotradeBot.id).where(AutotradeBot.broker_id == broker_id)
                )
            ).scalars().all()
        )
        for bid in bot_ids:
            await session.execute(delete(AutotradeBotTrade).where(AutotradeBotTrade.bot_id == bid))
            await session.execute(delete(AutotradeBotRun).where(AutotradeBotRun.bot_id == bid))
        await session.execute(delete(AutotradeBot).where(AutotradeBot.broker_id == broker_id))
        order_ids = list(
            (await session.execute(select(Order.id).where(Order.broker_id == broker_id)))
            .scalars().all()
        )
        if order_ids:
            await session.execute(delete(Fill).where(Fill.order_id.in_(order_ids)))
            await session.execute(delete(Order).where(Order.id.in_(order_ids)))
        await session.execute(delete(BrokerPosition).where(BrokerPosition.broker_id == broker_id))
        await session.execute(delete(BrokerAccount).where(BrokerAccount.broker_id == broker_id))
        await session.execute(delete(BrokerGrant).where(BrokerGrant.broker_id == broker_id))
        await session.execute(delete(Broker).where(Broker.id == broker_id))
        await session.execute(delete(User).where(User.id == user_id))
        await session.execute(delete(Role).where(Role.id == role_id))
        await session.execute(delete(MarketDataBar).where(MarketDataBar.symbol == SYMBOL))
        await session.commit()


def spec(broker_id, **over) -> BotSpec:
    base = dict(
        name="test bot", broker_id=broker_id, watchlist_id=None, symbols=[SYMBOL],
        market_type="regular", bar_interval="5m", max_trades_per_session=1,
        max_trades_per_day=2, capital_per_trade=Decimal("5000"), strategy_mode="single",
        setups=["stub_long"], min_score=3, stop_loss_mode="auto", stop_loss_max_pct=None,
        trailing_stop_pct=None, take_profit_mode="auto", take_profit_min_pct=None,
        trailing_take_profit_pct=None, news_blackout_minutes=0,
    )
    base.update(over)
    return BotSpec(**base)


async def new_run(session, bot, clock) -> AutotradeBotRun:
    run = AutotradeBotRun(
        id=uuid.uuid4(), bot_id=bot.id, status=AutotradeBotRunStatus.FAILED, started_at=clock()
    )
    session.add(run)
    await session.flush()
    return run


@pytest.mark.asyncio
async def test_opens_a_position_then_stops_out_then_respects_the_daily_limit(monkeypatch):
    monkeypatch.setitem(scanner_module.SETUPS, "stub_long", always_long)
    monkeypatch.setattr(
        "apps.api.app.autotrade.service.SETUPS", {**scanner_module.SETUPS, "stub_long": always_long}
    )
    settings = get_settings()
    router = StubRouter()
    # Eight bars of a quiet tape, 09:30 -> 10:05 ET, last close 100.
    prices = [
        Decimal(p) for p in ("101", "100.8", "100.6", "100.5", "100.4", "100.3", "100.1", "100")
    ]
    router.bars = bars(prices)
    now = OPEN_UTC + timedelta(minutes=40)  # 10:10 ET, regular hours

    async with db_session() as session, fixture(session) as (user_id, broker_id, _role):
        bot = await create_bot(session, spec(broker_id), user_id=user_id)
        await approve_bot(bot, approved_by_user_id=user_id)
        await session.commit()

        # --- cycle 1: a signal, one entry ---------------------------------
        run1 = await new_run(session, bot, lambda: now)
        out1 = await run_bot_cycle(
            session, bot, run1, settings=settings, clock=lambda: now, bar_router=router,
        )
        await session.commit()
        assert out1.status is AutotradeBotRunStatus.SUCCEEDED, out1.detail
        assert out1.symbols_scanned == 1 and out1.signals_found == 1
        assert out1.trades_opened == 1, out1.detail
        assert router.calls == 1  # bars were refreshed from the vendor

        trade = (
            await session.execute(
                select(AutotradeBotTrade).where(AutotradeBotTrade.bot_id == bot.id)
            )
        ).scalar_one()
        assert trade.closed_at is None
        assert trade.quantity == Decimal(50)  # 5000 / 100, whole shares
        assert trade.entry_price == Decimal("100")
        assert trade.initial_stop_price == Decimal("98.00")
        assert trade.take_profit_price == Decimal("104.00")  # 2R
        order = await session.get(Order, trade.entry_order_id)
        assert order is not None and order.autotrade_bot_run_id == run1.id

        # --- cycle 2: still open, a new signal is NOT taken (1 in live) ---
        router.bars = bars([*prices, Decimal("100.5")])
        now2 = now + timedelta(minutes=5)
        run2 = await new_run(session, bot, lambda: now2)
        out2 = await run_bot_cycle(
            session, bot, run2, settings=settings, clock=lambda: now2, bar_router=router,
        )
        await session.commit()
        assert out2.status is AutotradeBotRunStatus.SUCCEEDED
        assert out2.trades_opened == 0 and out2.trades_closed == 0
        assert out2.symbols_scanned == 0  # the held symbol is not re-scanned

        # --- cycle 3: the bar's low pierces the stop -> stop-loss exit ------
        router.bars = bars([*prices, Decimal("100.5"), Decimal("97.5")])
        now3 = now + timedelta(minutes=10)
        run3 = await new_run(session, bot, lambda: now3)
        out3 = await run_bot_cycle(
            session, bot, run3, settings=settings, clock=lambda: now3, bar_router=router,
        )
        await session.commit()
        assert out3.trades_closed == 1, out3.detail
        await session.refresh(trade)
        assert trade.closed_at is not None
        assert trade.exit_reason == "stop_loss"
        assert trade.exit_price == Decimal("97.5")
        assert trade.realized_pnl == Decimal("-125.0")  # (97.5 - 100) * 50
        assert trade.r_multiple == Decimal("-1.25")  # -2.5 / 2.0
        # The stub still fires on this bar, and one slot is free again, so a
        # second entry of the day is taken (daily limit is 2).
        assert out3.trades_opened == 1

        # --- cycle 4: second position stopped; a THIRD entry is refused by
        # the daily limit even though the stub keeps signalling ----------
        router.bars = bars([*prices, Decimal("100.5"), Decimal("97.5"), Decimal("95")])
        now4 = now + timedelta(minutes=15)
        run4 = await new_run(session, bot, lambda: now4)
        out4 = await run_bot_cycle(
            session, bot, run4, settings=settings, clock=lambda: now4, bar_router=router,
        )
        await session.commit()
        assert out4.trades_closed == 1
        assert out4.trades_opened == 0
        assert "allowance exhausted" in (out4.detail or "")
        opened_today = (
            await session.execute(
                select(AutotradeBotTrade).where(AutotradeBotTrade.bot_id == bot.id)
            )
        ).scalars().all()
        assert len(opened_today) == 2


@pytest.mark.asyncio
async def test_outside_the_traded_phase_nothing_is_read(monkeypatch):
    monkeypatch.setitem(scanner_module.SETUPS, "stub_long", always_long)
    monkeypatch.setattr(
        "apps.api.app.autotrade.service.SETUPS", {**scanner_module.SETUPS, "stub_long": always_long}
    )
    settings = get_settings()
    router = StubRouter()
    router.bars = bars([Decimal("100")] * 8)
    closed = datetime(2026, 6, 17, 2, 0, tzinfo=UTC)  # 22:00 ET the night before

    async with db_session() as session, fixture(session) as (user_id, broker_id, _role):
        bot = await create_bot(session, spec(broker_id), user_id=user_id)
        await approve_bot(bot, approved_by_user_id=user_id)
        run = await new_run(session, bot, lambda: closed)
        out = await run_bot_cycle(
            session, bot, run, settings=settings, clock=lambda: closed, bar_router=router,
        )
        await session.commit()
        assert out.status is AutotradeBotRunStatus.SKIPPED_MARKET_CLOSED
        assert router.calls == 0


@pytest.mark.asyncio
async def test_a_bot_that_is_not_active_does_nothing():
    settings = get_settings()
    router = StubRouter()
    now = OPEN_UTC + timedelta(minutes=40)
    async with db_session() as session, fixture(session) as (user_id, broker_id, _role):
        bot = await create_bot(session, spec(broker_id, strategy_mode="auto", setups=[]),
                               user_id=user_id)
        run = await new_run(session, bot, lambda: now)
        out = await run_bot_cycle(
            session, bot, run, settings=settings, clock=lambda: now, bar_router=router,
        )
        await session.commit()
        assert out.status is AutotradeBotRunStatus.SKIPPED_NOT_ACTIVE
        assert router.calls == 0


@pytest.mark.asyncio
async def test_no_vendor_is_a_not_configured_row_not_a_trade_on_stale_bars():
    settings = get_settings()
    now = OPEN_UTC + timedelta(minutes=40)
    async with db_session() as session, fixture(session) as (user_id, broker_id, _role):
        bot = await create_bot(session, spec(broker_id, strategy_mode="auto", setups=[]),
                               user_id=user_id)
        await approve_bot(bot, approved_by_user_id=user_id)
        run = await new_run(session, bot, lambda: now)
        out = await run_bot_cycle(
            session, bot, run, settings=settings, clock=lambda: now, bar_router=None,
        )
        await session.commit()
        assert out.status is AutotradeBotRunStatus.SKIPPED_NOT_CONFIGURED
