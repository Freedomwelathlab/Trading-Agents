"""Phase 83 learning loop (D099): per-symbol demotion, findings, journal.

Pure functions on in-memory trade rows; the journal writer is exercised
against the real database once.
"""

import contextlib
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from apps.api.app.autotrade.learning import (
    DEMOTE_MIN_TRADES,
    active_setups_for_symbol,
    build_session_insight,
    derive_findings,
    stats_by_setup,
    stats_by_setup_symbol,
    write_pending_insights,
)
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    AutotradeBot,
    AutotradeBotInsight,
    AutotradeBotTrade,
    Broker,
    BrokerKind,
)

D = Decimal
T0 = datetime(2026, 6, 17, 14, 0, tzinfo=UTC)  # 10:00 ET


def trade(
    *, setup="s", symbol="A.US", r, score=3, peak_r=None, trough_r=None,
    exit_reason="stop_loss", day=date(2026, 6, 17), hour_offset=0,
) -> AutotradeBotTrade:
    """A closed long with entry 100, stop 98 (1R = 2), exit at entry + 2R."""
    entry, risk = D("100"), D("2")
    exit_price = entry + risk * D(str(r))
    return AutotradeBotTrade(
        id=uuid.uuid4(), bot_id=uuid.uuid4(), symbol=symbol, session_date=day,
        setup_name=setup, score=score, evidence={}, quantity=D(10),
        entry_price=entry, initial_stop_price=entry - risk, stop_price=entry - risk,
        take_profit_price=entry + 2 * risk,
        peak_price=entry + risk * D(str(peak_r if peak_r is not None else max(r, 0))),
        trough_price=entry + risk * D(str(trough_r if trough_r is not None else min(r, 0))),
        take_profit_armed=False,
        opened_at=T0 + timedelta(hours=hour_offset),
        closed_at=T0 + timedelta(hours=hour_offset, minutes=30),
        exit_price=exit_price, exit_reason=exit_reason,
        realized_pnl=(exit_price - entry) * 10, r_multiple=D(str(r)),
    )


def test_a_setup_losing_on_one_symbol_is_demoted_only_there():
    losers_a = [trade(symbol="A.US", r=-1) for _ in range(DEMOTE_MIN_TRADES)]
    winners_b = [trade(symbol="B.US", r=1) for _ in range(DEMOTE_MIN_TRADES)]
    all_trades = losers_a + winners_b
    by_setup, by_pair = stats_by_setup(all_trades), stats_by_setup_symbol(all_trades)
    # Overall expectancy is 0 -> not demoted at the setup level...
    assert not by_setup[0].demoted
    # ...but the (s, A.US) pair has its own 20 losers.
    kw = dict(strategy_mode="auto", by_setup=by_setup, by_pair=by_pair)
    assert active_setups_for_symbol(["s"], symbol="A.US", **kw) == []
    assert active_setups_for_symbol(["s"], symbol="B.US", **kw) == ["s"]
    # A symbol with no history inherits the setup-level verdict.
    assert active_setups_for_symbol(["s"], symbol="C.US", **kw) == ["s"]


def test_a_new_symbol_inherits_a_setup_level_demotion():
    losers = [trade(symbol="A.US", r=-1) for _ in range(DEMOTE_MIN_TRADES)]
    few_wins = [trade(symbol="B.US", r=1) for _ in range(3)]  # too few to override
    all_trades = losers + few_wins
    kw = dict(
        strategy_mode="auto", by_setup=stats_by_setup(all_trades),
        by_pair=stats_by_setup_symbol(all_trades),
    )
    assert active_setups_for_symbol(["s"], symbol="B.US", **kw) == []
    assert active_setups_for_symbol(["s"], symbol="Z.US", **kw) == []
    # Explicit modes are never overridden.
    explicit = {**kw, "strategy_mode": "multi"}
    assert active_setups_for_symbol(["s"], symbol="A.US", **explicit) == ["s"]


def test_stops_too_tight_finding_needs_losers_that_first_reached_one_r():
    reached = [trade(r=-1, peak_r=1.2) for _ in range(6)]
    straight = [trade(r=-1, peak_r=0.1) for _ in range(2)]
    f = derive_findings(reached + straight, reached + straight, min_score=3)
    assert any("reached +1R before losing" in x for x in f)
    # Straight-to-stop losers produce the opposite finding instead.
    f2 = derive_findings(straight * 4, straight * 4, min_score=3)
    assert any("never got past +0.25R" in x for x in f2)
    assert not any("reached +1R" in x for x in f2)


def test_score_band_finding_needs_ten_of_each_and_opposite_signs():
    low = [trade(r=-0.5, score=3) for _ in range(10)]
    high = [trade(r=0.8, score=4) for _ in range(10)]
    f = derive_findings(low + high, low + high, min_score=3)
    assert any("raising the minimum score to 4" in x for x in f)
    small = derive_findings(low[:5] + high[:5], low[:5] + high[:5], min_score=3)
    assert not any("minimum score" in x for x in small)


def test_session_end_dominance_and_losing_hour_findings():
    se = [trade(r=0.1, exit_reason="session_end") for _ in range(6)]
    f = derive_findings(se, se, min_score=3)
    assert any("session-end flats" in x for x in f)
    bad_hour = [trade(r=-0.6, hour_offset=5) for _ in range(5)]  # 15:00 ET
    f2 = derive_findings(bad_hour, bad_hour, min_score=3)
    assert any("15:00 ET hour" in x for x in f2)


def test_session_insight_aggregates_the_day_and_ranks_setups():
    day = date(2026, 6, 17)
    trades = [
        trade(setup="good", r=1, day=day), trade(setup="good", r=0.5, day=day),
        trade(setup="bad", r=-1, day=day), trade(setup="other", r=2, day=date(2026, 6, 16)),
    ]
    ins = build_session_insight(day, trades, min_score=3)
    assert ins.trades == 3 and ins.wins == 2
    assert ins.total_r == D("0.5")
    assert ins.best_setup == "good" and ins.worst_setup == "bad"


@contextlib.asynccontextmanager
async def db_session():
    gen = get_session()
    s = await anext(gen)
    try:
        yield s
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_journal_writes_past_sessions_once_and_today_only_when_asked():
    async with db_session() as session:
        broker = Broker(id=uuid.uuid4(), name="j", kind=BrokerKind.PAPER, provider="paper")
        bot = AutotradeBot(
            id=uuid.uuid4(), name="j", broker_id=broker.id, symbols=["A.US"],
            max_trades_per_session=1, max_trades_per_day=1, capital_per_trade=D(1000),
            setups=["s"],
        )
        session.add(broker)
        await session.flush()  # no ORM relationship: order the FK parent explicitly
        session.add(bot)
        await session.flush()
        yesterday, today = date(2026, 6, 16), date(2026, 6, 17)
        for t in (trade(r=1, day=yesterday), trade(r=-1, day=today)):
            t.bot_id = bot.id
            session.add(t)
        await session.flush()
        try:
            first = await write_pending_insights(session, bot.id, min_score=3, before=today)
            assert [w.session_date for w in first] == [yesterday]
            again = await write_pending_insights(session, bot.id, min_score=3, before=today)
            assert again == []
            closing = await write_pending_insights(
                session, bot.id, min_score=3, before=today, include=today
            )
            assert [w.session_date for w in closing] == [today]
            rows = (
                await session.execute(
                    select(AutotradeBotInsight).where(AutotradeBotInsight.bot_id == bot.id)
                )
            ).scalars().all()
            assert {r.session_date for r in rows} == {yesterday, today}
        finally:
            await session.rollback()
            await session.execute(
                delete(AutotradeBotTrade).where(AutotradeBotTrade.bot_id == bot.id)
            )
            await session.execute(
                delete(AutotradeBotInsight).where(AutotradeBotInsight.bot_id == bot.id)
            )
            await session.execute(delete(AutotradeBot).where(AutotradeBot.id == bot.id))
            await session.execute(delete(Broker).where(Broker.id == broker.id))
            await session.commit()
