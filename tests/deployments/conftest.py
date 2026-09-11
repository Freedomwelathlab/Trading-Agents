"""Shared builders for the Phase 63 deployment tests (D081).

Everything here is real: a real paper `Broker` row, a real validated
`StrategyVersion`, real `market_data_bars`. The only thing these tests do
NOT exercise is wall-clock scheduling - `run_deployment_cycle` is awaited
directly for a deterministic single pass, exactly as the reconciler tests
call `run_reconciliation_cycle`.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    Broker,
    BrokerAccount,
    BrokerKind,
    BrokerPosition,
    Fill,
    Order,
    SignalEvaluation,
    Strategy,
    StrategyDeployment,
    StrategyDeploymentRun,
    StrategyStatus,
    StrategyVersion,
    StrategyVersionStatus,
)
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.store import MarketDataStore

# SMA(2): `close crosses_above sma_2` reduces to "close rose above the
# recent average" - a series that turns produces a clean entry then exit.
SMA2 = {
    "indicators": [{"id": "sma_2", "type": "sma", "period": 2}],
    "entry_rule": {"op": "crosses_above", "left": "close", "right": "sma_2"},
    "exit_rule": {"op": "crosses_below", "left": "close", "right": "sma_2"},
    "position_sizing": {"type": "all_in"},
}

# Ends on a fresh cross UP (100 -> 108 with sma_2 at 104): a BUY on the
# latest bar.
BUY_CLOSES = [100, 105, 100, 105, 100, 108]
# Ends on a fresh cross DOWN: a SELL on the latest bar.
SELL_CLOSES = [100, 95, 100, 95, 100, 92]

_FIRST_BAR = date(2026, 2, 2)


@contextlib.asynccontextmanager
async def db_session():
    gen = get_session()
    session = await anext(gen)
    try:
        yield session
    finally:
        await gen.aclose()


def _weekday_bars(symbol: str, closes: list[int]) -> list[Bar]:
    bars: list[Bar] = []
    day = _FIRST_BAR
    i = 0
    while i < len(closes):
        if day.weekday() < 5:
            bars.append(
                Bar(
                    symbol=symbol,
                    bar_interval="1d",
                    ts=datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
                    close=Decimal(closes[i]),
                    source="test-deploy",
                )
            )
            i += 1
        day += timedelta(days=1)
    return bars


@contextlib.asynccontextmanager
async def deployment_world(
    session,
    *,
    seed: dict[str, list[int]] | None = None,
    definition: dict | None = None,
    starting_cash: Decimal = Decimal(100_000),
    with_live_broker: bool = False,
):
    """A paper broker, a validated version, and any seeded bars - torn down
    child-first because of the ON DELETE RESTRICT FKs.

    `with_live_broker=True` (Phase 64, D082) additionally creates a LIVE
    broker and returns its id as `live_broker_id`, for the live-mode
    deployment tests. It carries no `BrokerAccount`: an UNARMED live cycle
    never needs one (it resolves to `SKIPPED_LIVE_TRADING_DISABLED` first),
    and an ARMED one (Phase 69, D087) gets its book from the live broker
    adapter rather than from a local `BrokerAccount` row - which is exactly
    why `runner.py` never calls `save_paper_broker` for a live deployment."""
    prefix = f"DEP{uuid.uuid4().hex[:6].upper()}"
    broker_id = uuid.uuid4()
    live_broker_id = uuid.uuid4() if with_live_broker else None
    strategy_id = uuid.uuid4()
    version_id = uuid.uuid4()
    seed = seed or {}
    mapping = {name: f"{prefix}{name.upper()}.US" for name in seed}

    session.add(Broker(id=broker_id, name=prefix, kind=BrokerKind.PAPER, provider="paper-sim"))
    session.add(BrokerAccount(broker_id=broker_id, cash=starting_cash))
    if live_broker_id is not None:
        session.add(
            Broker(
                id=live_broker_id,
                name=f"{prefix}L",
                kind=BrokerKind.LIVE,
                provider="longbridge",
            )
        )
    session.add(
        Strategy(id=strategy_id, name=prefix, owner_user_id=None, status=StrategyStatus.ACTIVE)
    )
    await session.flush()
    session.add(
        StrategyVersion(
            id=version_id,
            strategy_id=strategy_id,
            version_number=1,
            definition=definition or SMA2,
            definition_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            status=StrategyVersionStatus.VALIDATED,
        )
    )
    store = MarketDataStore(session)
    for name, closes in seed.items():
        await store.upsert_bars(_weekday_bars(mapping[name], closes))
    await session.commit()

    try:
        yield {
            "broker_id": broker_id,
            "live_broker_id": live_broker_id,
            "strategy_id": strategy_id,
            "version_id": version_id,
            "symbols": mapping,
        }
    finally:
        await session.rollback()
        dep_ids = select(StrategyDeployment.id).where(
            StrategyDeployment.strategy_version_id == version_id
        )
        run_ids = select(StrategyDeploymentRun.id).where(
            StrategyDeploymentRun.deployment_id.in_(dep_ids)
        )
        await session.execute(
            delete(SignalEvaluation).where(SignalEvaluation.strategy_version_id == version_id)
        )
        # Both brokers' orders, not just the paper one: since Phase 69
        # (D087) an ARMED live deployment really does place orders against
        # the live broker, so a teardown that only cleaned up the paper
        # broker's rows would hit orders_broker_id_fkey on the way out.
        broker_ids = [b for b in (broker_id, live_broker_id) if b is not None]
        await session.execute(
            delete(Fill).where(
                Fill.order_id.in_(select(Order.id).where(Order.broker_id.in_(broker_ids)))
            )
        )
        await session.execute(delete(Order).where(Order.broker_id.in_(broker_ids)))
        await session.execute(
            delete(StrategyDeploymentRun).where(StrategyDeploymentRun.id.in_(run_ids))
        )
        await session.execute(
            delete(StrategyDeployment).where(StrategyDeployment.id.in_(dep_ids))
        )
        await session.execute(
            delete(BrokerPosition).where(BrokerPosition.broker_id.in_(broker_ids))
        )
        await session.execute(
            delete(BrokerAccount).where(BrokerAccount.broker_id.in_(broker_ids))
        )
        await session.execute(
            delete(SignalEvaluation).where(SignalEvaluation.symbol.in_(list(mapping.values())))
        )
        from apps.api.app.db.models import MarketDataBar

        await session.execute(
            delete(MarketDataBar).where(MarketDataBar.symbol.in_(list(mapping.values())))
        )
        await session.execute(delete(StrategyVersion).where(StrategyVersion.id == version_id))
        await session.execute(delete(Strategy).where(Strategy.id == strategy_id))
        await session.execute(delete(Broker).where(Broker.id == broker_id))
        if live_broker_id is not None:
            await session.execute(delete(Broker).where(Broker.id == live_broker_id))
        await session.commit()
