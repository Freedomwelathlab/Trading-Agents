"""Paper-trading rehearsal for Strategy Library bots (Phase 109, D136).

Creates a throwaway PAPER broker in the database `DATABASE_URL` points at,
puts one library bot on each market on it, approves them, and runs real
cycles: real adjusted history and real delayed quotes from the library's
vendor, the real Risk Engine, the real paper order path. Then it prints
what each bot did and deletes everything it created.

    python -m scripts.library_paper_rehearsal            # run, report, clean up
    python -m scripts.library_paper_rehearsal --keep     # leave the rows to inspect

What it proves: the execution path, end to end, on live data. What it does
not prove: that any strategy makes money - a rehearsal has no track record.
A market that is closed when this runs is reported as such (its order is
skipped on a stale quote), which is the correct behaviour, not a failure.

Never point this at a production database: it writes and deletes rows.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select

from apps.api.app.core.config import get_settings
from apps.api.app.db.base import get_session_factory
from apps.api.app.db.models import (
    Broker,
    BrokerAccount,
    BrokerGrant,
    BrokerKind,
    BrokerPosition,
    Fill,
    LibraryBot,
    LibraryBotRun,
    Order,
    User,
)
from apps.api.app.execution.persistence import load_paper_broker
from apps.api.app.library.bot_runner import run_library_bot_cycle_all
from apps.api.app.library.bot_service import (
    LibraryBotSpec,
    approve_library_bot,
    create_library_bot,
)
from apps.api.app.marketdata.providers.yahoo import YahooDailyProvider

SPECS: tuple[tuple[str, str, list[str], dict, str], ...] = (
    ("US  SPY 200-day trend", "trend_200d", ["SPY.US"], {}, "8000"),
    ("US  asset-class rotation", "momentum_rotation",
     ["SPY.US", "EFA.US", "IEF.US", "VNQ.US", "GLD.US"], {"top_n": 2}, "16000"),
    ("SG  STI ETF 200-day trend", "trend_200d", ["ES3.SG"], {}, "8000"),
    ("IN  Nifty ETF 200-day trend", "trend_200d", ["NIFTYBEES.IN"], {}, "8000"),
    ("CR  BTC/ETH/SOL Donchian", "donchian_breakout",
     ["BTC-USD", "ETH-USD", "SOL-USD"], {}, "9000"),
    ("CR  BTC 200-day trend", "trend_200d", ["BTC-USD"], {}, "8000"),
    ("FX  majors 12m momentum", "tsmom_12m",
     ["EURUSD.FX", "GBPUSD.FX", "AUDUSD.FX"], {}, "9000"),
)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="do not delete the rehearsal rows")
    args = parser.parse_args()

    settings = get_settings()
    if settings.live_trading_enabled:
        print("Refusing to run: LIVE_TRADING_ENABLED is on in this environment.")
        return 2
    factory = get_session_factory()
    feed = YahooDailyProvider()
    broker_id = uuid.uuid4()
    started = datetime.now(UTC)

    async with factory() as session:
        user = (
            await session.execute(select(User).order_by(User.created_at).limit(1))
        ).scalar_one_or_none()
        if user is None:
            print("No user in this database; create one first (scripts/create_admin.py).")
            return 2
        session.add(
            Broker(id=broker_id, name=f"library-rehearsal-{started:%H%M%S}",
                   kind=BrokerKind.PAPER, provider="paper", is_active=True)
        )
        session.add(BrokerGrant(id=uuid.uuid4(), user_id=user.id, broker_id=broker_id))
        await session.flush()
        bots: list[tuple[str, uuid.UUID]] = []
        for label, strategy_id, symbols, params, capital in SPECS:
            bot = await create_library_bot(
                session,
                LibraryBotSpec(
                    name=label, broker_id=broker_id, strategy_id=strategy_id, symbols=symbols,
                    params=params, capital=Decimal(capital), max_drawdown_pct=Decimal(25),
                ),
                user_id=user.id,
            )
            await approve_library_bot(bot, approved_by_user_id=user.id)
            bots.append((label, bot.id))
        await session.commit()

    print(f"Paper broker {broker_id} with {settings.paper_broker_starting_cash} starting cash, "
          f"{len(bots)} bots approved. Clock: {started:%Y-%m-%d %H:%M} UTC.\n")
    try:
        for round_no in (1, 2):
            print(f"--- cycle {round_no} " + "-" * 60)
            for label, bot_id in bots:
                result = await run_library_bot_cycle_all(
                    factory, settings=settings, provider=feed, quotes=feed, only_bot_id=bot_id
                )
                outcome = result.outcomes[0] if result.outcomes else None
                if outcome is None:
                    print(f"{label:<28} no outcome")
                    continue
                equity = f"{outcome.equity:.2f}" if outcome.equity is not None else "n/a"
                print(f"{label:<28} {outcome.status.value:<10} filled={outcome.orders_filled} "
                      f"rejected={outcome.orders_rejected} equity={equity}")
                print(f"    {outcome.detail}")
                async with factory() as session:
                    run = await session.get(LibraryBotRun, outcome.run_id)
                    for order in (run.orders if run and run.orders else []):
                        note = f" - {order['detail']}" if order.get("detail") else ""
                        print(
                            f"    {order['outcome']:<8} {order['side']:<4} "
                            f"{order['quantity']:>14} {order['symbol']:<12} "
                            f"@ {order['price']:<12} "
                            f"quote {order['quote_as_of'][11:16]}Z{note}"
                        )
            print()

        async with factory() as session:
            paper = await load_paper_broker(
                session, broker_id, default_starting_cash=settings.paper_broker_starting_cash
            )
            orders = (
                await session.execute(select(Order).where(Order.broker_id == broker_id))
            ).scalars().all()
            print("--- paper account after both cycles " + "-" * 40)
            print(f"cash {paper.cash:.2f}; {len(orders)} order rows "
                  f"({sum(1 for o in orders if o.status.value == 'filled')} filled)")
            for symbol, quantity in sorted(paper.positions.items()):
                print(f"    {symbol:<14} {quantity}")
    finally:
        if not args.keep:
            async with factory() as session:
                bot_ids = [bid for _, bid in bots]
                await session.execute(
                    delete(LibraryBotRun).where(LibraryBotRun.bot_id.in_(bot_ids))
                )
                await session.execute(delete(LibraryBot).where(LibraryBot.broker_id == broker_id))
                order_ids = select(Order.id).where(Order.broker_id == broker_id)
                await session.execute(delete(Fill).where(Fill.order_id.in_(order_ids)))
                await session.execute(delete(Order).where(Order.broker_id == broker_id))
                await session.execute(
                    delete(BrokerPosition).where(BrokerPosition.broker_id == broker_id)
                )
                await session.execute(
                    delete(BrokerAccount).where(BrokerAccount.broker_id == broker_id)
                )
                await session.execute(delete(BrokerGrant).where(BrokerGrant.broker_id == broker_id))
                await session.execute(delete(Broker).where(Broker.id == broker_id))
                await session.commit()
            print("\nrehearsal rows deleted (run with --keep to inspect them)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
