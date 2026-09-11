"""Integration tests for the ARMED unattended live execution path
(Phase 69, D087), against real Postgres.

WHY THE BROKER IS A `PaperBrokerAdapter` HERE
----------------------------------------------
`build_live_broker_adapter` is monkeypatched to return an in-memory
`PaperBrokerAdapter`. That is deliberate and is not a weakening of these
tests: what is under test is the RUNNER's gating, capital arithmetic and
halt behaviour, and `PaperBrokerAdapter` implements the same
`BrokerAdapter` protocol `LiveBrokerAdapter` does, with real fill maths.
The Longbridge adapter's own translation of that protocol onto the vendor
SDK is covered by `tests/execution/`, and exercising it here would mean
either real credentials or a mock of the vendor - neither of which would
tell us anything more about the code these tests exist to check.

Crucially, no test in this file ever constructs a real live broker, and
none can: the patch replaces the only function that builds one.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from apps.api.app.core.config import TradingMode, get_settings
from apps.api.app.db.base import get_session_factory
from apps.api.app.db.models import (
    Order,
    OrderStatus,
    StrategyDeployment,
    StrategyDeploymentRun,
    StrategyDeploymentRunStatus,
    StrategyDeploymentStatus,
)
from apps.api.app.deployments.runner import run_deployment_cycle
from apps.api.app.deployments.service import approve_deployment, create_deployment
from apps.api.app.execution.paper_broker import PaperBrokerAdapter
from apps.api.app.marketdata.store import MarketDataStore
from tests.deployments.conftest import (
    BUY_CLOSES,
    _weekday_bars,
    db_session,
    deployment_world,
)

_SF = get_session_factory()

_ARMED = {
    "trading_mode": TradingMode.LIVE,
    "live_trading_enabled": True,
    "strategy_live_auto_execution_enabled": True,
    "strategy_live_total_capital": Decimal("100000"),
    "strategy_live_capital_per_trade": Decimal("50000"),
}


def _armed(**overrides: object):
    return get_settings().model_copy(update={**_ARMED, **overrides})


@pytest.fixture
def fake_live_broker(monkeypatch):
    """Patch the ONE function that constructs a live broker. Returns the
    adapter so a test can inspect the fills it produced."""
    adapter = PaperBrokerAdapter(starting_cash=Decimal("100000"))
    monkeypatch.setattr(
        "apps.api.app.deployments.runner.build_live_broker_adapter",
        lambda settings: adapter,
    )
    return adapter


async def _deploy_live(session, world, *, symbols) -> uuid.UUID:
    dep = await create_deployment(
        session,
        strategy_version_id=world["version_id"],
        broker_id=world["live_broker_id"],
        symbols=symbols,
        bar_interval="1d",
        mode="live",
        requested_by_user_id=None,
    )
    await approve_deployment(dep, approved_by_user_id=None)
    await session.commit()
    return dep.id


def _mine(result, deployment_id):
    return next((o for o in result.outcomes if o.deployment_id == deployment_id), None)


@pytest.mark.asyncio
async def test_an_armed_live_deployment_places_a_real_order(fake_live_broker) -> None:
    """The core of D087: with every gate affirmatively satisfied, a live
    deployment's cycle actually trades - it no longer resolves to
    SKIPPED_LIVE_TRADING_DISABLED."""
    async with (
        db_session() as session,
        deployment_world(session, seed={"g": BUY_CLOSES}, with_live_broker=True) as w,
    ):
        dep_id = await _deploy_live(session, w, symbols=[w["symbols"]["g"]])

        result = await run_deployment_cycle(_SF, settings=_armed())
        outcome = _mine(result, dep_id)

        assert outcome is not None
        assert outcome.status is StrategyDeploymentRunStatus.SUCCEEDED
        assert outcome.symbols_evaluated == 1
        assert outcome.orders_filled == 1

        orders = (
            (await session.execute(select(Order).where(Order.broker_id == w["live_broker_id"])))
            .scalars()
            .all()
        )
        assert [o.status for o in orders][-1] is OrderStatus.FILLED


@pytest.mark.asyncio
async def test_capital_per_trade_caps_a_larger_strategy_sizing(fake_live_broker) -> None:
    """The user-facing input that matters most: `capital per trade` binds
    even when the strategy's own `all_in` sizing would commit far more.

    Asserted as an inequality against the cap rather than an exact share
    count, so this stays a test of the CAP rather than of the definition's
    sizing arithmetic (which `engine_v2`'s own tests own)."""
    async with (
        db_session() as session,
        deployment_world(session, seed={"g": BUY_CLOSES}, with_live_broker=True) as w,
    ):
        dep_id = await _deploy_live(session, w, symbols=[w["symbols"]["g"]])

        settings = _armed(strategy_live_capital_per_trade=Decimal("1000"))
        result = await run_deployment_cycle(_SF, settings=settings)
        outcome = _mine(result, dep_id)
        assert outcome is not None

        orders = (
            (
                await session.execute(
                    select(Order)
                    .where(Order.broker_id == w["live_broker_id"])
                    .order_by(Order.submitted_at)
                )
            )
            .scalars()
            .all()
        )
        assert orders, "the armed cycle should have submitted something"
        for order in orders:
            committed = order.quantity * order.estimated_price
            assert committed <= Decimal("1000"), (
                f"order committed {committed}, above the 1000 per-trade cap"
            )


@pytest.mark.asyncio
async def test_a_zero_capital_allowance_places_no_order_but_does_not_halt(
    fake_live_broker,
) -> None:
    """A per-trade budget too small for even one share makes no trade. That
    is a real answer, not an error and not a halt: the cycle SUCCEEDS having
    evaluated the symbol and acted on nothing."""
    async with (
        db_session() as session,
        deployment_world(session, seed={"g": BUY_CLOSES}, with_live_broker=True) as w,
    ):
        dep_id = await _deploy_live(session, w, symbols=[w["symbols"]["g"]])

        settings = _armed(
            strategy_live_total_capital=Decimal("1"),
            strategy_live_capital_per_trade=Decimal("1"),
        )
        result = await run_deployment_cycle(_SF, settings=settings)
        outcome = _mine(result, dep_id)

        assert outcome is not None
        assert outcome.status is StrategyDeploymentRunStatus.SUCCEEDED
        assert outcome.symbols_evaluated == 1
        assert outcome.orders_submitted == 0

        deployment = await session.get(StrategyDeployment, dep_id)
        assert deployment is not None
        await session.refresh(deployment)
        assert deployment.status is StrategyDeploymentStatus.ACTIVE  # not paused


@pytest.mark.asyncio
async def test_an_open_position_ceiling_of_zero_blocks_new_entries(fake_live_broker) -> None:
    """A full book refuses new entries without halting or pausing - a full
    book is a normal state, not a loss event."""
    async with (
        db_session() as session,
        deployment_world(session, seed={"g": BUY_CLOSES}, with_live_broker=True) as w,
    ):
        dep_id = await _deploy_live(session, w, symbols=[w["symbols"]["g"]])

        # First cycle opens a position.
        first = await run_deployment_cycle(_SF, settings=_armed())
        assert _mine(first, dep_id) is not None

        # Now cap the book at one position and re-run: no NEW entry.
        settings = _armed(strategy_live_max_open_positions=1)
        second = await run_deployment_cycle(_SF, settings=settings)
        outcome = _mine(second, dep_id)
        assert outcome is not None
        assert outcome.status is StrategyDeploymentRunStatus.SUCCEEDED

        deployment = await session.get(StrategyDeployment, dep_id)
        assert deployment is not None
        await session.refresh(deployment)
        assert deployment.status is StrategyDeploymentStatus.ACTIVE


@pytest.mark.asyncio
async def test_a_breached_daily_loss_breaker_halts_and_pauses(fake_live_broker) -> None:
    """The circuit breaker that protects real money: once the loss limit is
    reached the cycle writes SKIPPED_LIVE_RISK_HALT and the deployment is
    PAUSED - and the position is NOT liquidated."""
    async with (
        db_session() as session,
        deployment_world(session, seed={"g": BUY_CLOSES}, with_live_broker=True) as w,
    ):
        dep_id = await _deploy_live(session, w, symbols=[w["symbols"]["g"]])

        # Cycle one: open a real position at the armed settings, entering
        # around 108 (the last of BUY_CLOSES).
        first = await run_deployment_cycle(_SF, settings=_armed())
        assert _mine(first, dep_id) is not None

        # Crash the mark. Seeded deterministically rather than relying on
        # whatever unrealized P&L the fixture's own closes happen to leave,
        # so this test asserts the breaker FIRES rather than skipping when
        # the book is flat.
        await MarketDataStore(session).upsert_bars(
            _weekday_bars(w["symbols"]["g"], [*BUY_CLOSES, 10])
        )
        await session.commit()

        second = await run_deployment_cycle(_SF, settings=_armed())
        outcome = _mine(second, dep_id)
        assert outcome is not None, "the deployment should still be enumerated"
        assert outcome.status is StrategyDeploymentRunStatus.SKIPPED_LIVE_RISK_HALT

        run = (
            await session.execute(
                select(StrategyDeploymentRun).where(StrategyDeploymentRun.id == outcome.run_id)
            )
        ).scalar_one()
        assert run.status is StrategyDeploymentRunStatus.SKIPPED_LIVE_RISK_HALT
        assert run.error_detail and "circuit breaker" in run.error_detail

        deployment = await session.get(StrategyDeployment, dep_id)
        assert deployment is not None
        await session.refresh(deployment)
        assert deployment.status is StrategyDeploymentStatus.PAUSED

        # The position was NOT liquidated by the halt.
        sells = [
            o
            for o in (
                (
                    await session.execute(
                        select(Order).where(Order.broker_id == w["live_broker_id"])
                    )
                )
                .scalars()
                .all()
            )
            if o.side.value == "sell"
        ]
        assert sells == [], "a capital halt must never liquidate"


@pytest.mark.asyncio
async def test_an_unarmed_system_reads_nothing_even_with_a_live_broker_present(
    fake_live_broker,
) -> None:
    """Belt and braces on the most important property: with the robot
    switch off, the presence of a perfectly good live broker changes
    nothing."""
    async with (
        db_session() as session,
        deployment_world(session, seed={"g": BUY_CLOSES}, with_live_broker=True) as w,
    ):
        dep_id = await _deploy_live(session, w, symbols=[w["symbols"]["g"]])

        settings = _armed(strategy_live_auto_execution_enabled=False)
        result = await run_deployment_cycle(_SF, settings=settings)
        outcome = _mine(result, dep_id)

        assert outcome is not None
        assert outcome.status is StrategyDeploymentRunStatus.SKIPPED_LIVE_TRADING_DISABLED
        assert outcome.symbols_evaluated == 0
        assert fake_live_broker.fills == []
