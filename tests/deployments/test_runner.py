"""Integration tests for the strategy-deployment runner (Phase 63, D081),
against real Postgres.

`run_deployment_cycle` is awaited directly for one deterministic pass - the
same way tests/api/test_live_order_reconciler.py drives its cycle. Nothing
is patched: the real signal engine, the real Risk Engine, the real
trade-path Portfolio Manager, the real `PaperBrokerAdapter` fill math and
the real persistence layer all run.

The database is shared with every other test module, so a cycle sees every
ACTIVE deployment any test left behind. Each test here therefore filters
the cycle result to the deployment it created (`_mine`) and asserts on the
STRUCTURAL guarantees for that one - only ACTIVE deployments act, the run
row always resolves, the emergency stop halts it, an unpriceable held
position fails visibly, a second cycle is idempotent - never on a global
count.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from apps.api.app.core.config import TradingMode, get_settings
from apps.api.app.db.base import get_session_factory
from apps.api.app.db.models import (
    BacktestRun,
    BacktestRunStatus,
    BrokerPosition,
    DriftCheckStatus,
    Order,
    OrderStatus,
    SignalEvaluation,
    StrategyDeployment,
    StrategyDeploymentRun,
    StrategyDeploymentRunStatus,
    StrategyDeploymentStatus,
    StrategyDriftCheck,
)
from apps.api.app.deployments.monitoring import build_deployment_monitoring
from apps.api.app.deployments.runner import DeploymentCycleStatus, run_deployment_cycle
from apps.api.app.deployments.service import approve_deployment, create_deployment
from apps.api.app.marketdata.store import MarketDataStore
from apps.api.app.portfolio.market_hours import MarketHoursGate
from apps.api.app.safety.emergency_stop import set_emergency_stop
from tests.deployments.conftest import (
    BUY_CLOSES,
    SELL_CLOSES,
    _weekday_bars,
    db_session,
    deployment_world,
)

_DRIFT_BACKTEST_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

_SF = get_session_factory()
_SETTINGS = get_settings()


async def _deploy(
    session, world, *, symbols, approved=True, broker_id=None, mode="paper"
) -> uuid.UUID:
    dep = await create_deployment(
        session,
        strategy_version_id=world["version_id"],
        broker_id=broker_id if broker_id is not None else world["broker_id"],
        symbols=symbols,
        bar_interval="1d",
        mode=mode,
        requested_by_user_id=None,
    )
    if approved:
        await approve_deployment(dep, approved_by_user_id=None)
    await session.commit()
    return dep.id


def _mine(result, deployment_id):
    hits = [o for o in result.outcomes if o.deployment_id == deployment_id]
    return hits[0] if hits else None


@pytest.mark.asyncio
async def test_a_buy_signal_places_and_fills_a_paper_order_linked_to_the_run() -> None:
    async with db_session() as session, deployment_world(session, seed={"g": BUY_CLOSES}) as w:
        good = w["symbols"]["g"]
        dep_id = await _deploy(session, w, symbols=[good])

        result = await run_deployment_cycle(_SF, settings=_SETTINGS)
        assert result.status is DeploymentCycleStatus.RAN

        outcome = _mine(result, dep_id)
        assert outcome is not None
        assert outcome.status is StrategyDeploymentRunStatus.SUCCEEDED
        assert outcome.symbols_evaluated == 1
        assert outcome.signals_actionable == 1
        assert outcome.orders_filled == 1

        run = (
            await session.execute(
                select(StrategyDeploymentRun).where(StrategyDeploymentRun.id == outcome.run_id)
            )
        ).scalar_one()
        assert run.status is StrategyDeploymentRunStatus.SUCCEEDED
        assert run.completed_at is not None

        orders = (
            (
                await session.execute(
                    select(Order)
                    .where(Order.broker_id == w["broker_id"])
                    .order_by(Order.submitted_at)
                )
            )
            .scalars()
            .all()
        )
        # all_in is risk-rejected once, retried at the engine's cap, filled.
        assert orders[-1].status is OrderStatus.FILLED
        assert all(o.deployment_run_id == run.id for o in orders)

        position = (
            await session.execute(
                select(BrokerPosition).where(BrokerPosition.broker_id == w["broker_id"])
            )
        ).scalar_one()
        assert position.symbol == good and position.quantity > 0

        signals = (
            (
                await session.execute(
                    select(SignalEvaluation).where(SignalEvaluation.deployment_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert [s.symbol for s in signals] == [good]
        assert signals[0].signal.value == "buy"


@pytest.mark.asyncio
async def test_a_second_cycle_is_idempotent_when_already_positioned() -> None:
    async with db_session() as session, deployment_world(session, seed={"g": BUY_CLOSES}) as w:
        dep_id = await _deploy(session, w, symbols=[w["symbols"]["g"]])

        first = await run_deployment_cycle(_SF, settings=_SETTINGS)
        assert _mine(first, dep_id).orders_filled == 1

        second = await run_deployment_cycle(_SF, settings=_SETTINGS)
        outcome = _mine(second, dep_id)
        assert outcome.status is StrategyDeploymentRunStatus.SUCCEEDED
        assert outcome.signals_actionable == 0  # BUY while already long -> no action
        assert outcome.orders_submitted == 0

        runs = (
            (
                await session.execute(
                    select(StrategyDeploymentRun).where(
                        StrategyDeploymentRun.deployment_id == dep_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(runs) == 2  # every cycle writes a row, even the no-op one


@pytest.mark.asyncio
async def test_a_sell_signal_closes_an_open_position() -> None:
    async with db_session() as session, deployment_world(session, seed={"g": BUY_CLOSES}) as w:
        good = w["symbols"]["g"]
        dep_id = await _deploy(session, w, symbols=[good])
        first = await run_deployment_cycle(_SF, settings=_SETTINGS)
        assert _mine(first, dep_id).orders_filled == 1

        # Re-seed the same symbol so the latest bar is now a cross DOWN.
        await MarketDataStore(session).upsert_bars(_weekday_bars(good, SELL_CLOSES))
        await session.commit()

        result = await run_deployment_cycle(_SF, settings=_SETTINGS)
        outcome = _mine(result, dep_id)
        assert outcome.signals_actionable == 1
        assert outcome.orders_filled == 1

        positions = (
            (
                await session.execute(
                    select(BrokerPosition).where(BrokerPosition.broker_id == w["broker_id"])
                )
            )
            .scalars()
            .all()
        )
        assert positions == []  # fully closed


@pytest.mark.asyncio
async def test_the_global_emergency_stop_skips_the_cycle_and_places_nothing() -> None:
    async with db_session() as session, deployment_world(session, seed={"g": BUY_CLOSES}) as w:
        dep_id = await _deploy(session, w, symbols=[w["symbols"]["g"]])

        await set_emergency_stop(session, active=True, reason="test", actor_user_id=None)
        await session.commit()
        try:
            result = await run_deployment_cycle(_SF, settings=_SETTINGS)
            outcome = _mine(result, dep_id)
            assert outcome.status is StrategyDeploymentRunStatus.SKIPPED_EMERGENCY_STOP
            assert outcome.orders_submitted == 0

            orders = (
                (
                    await session.execute(
                        select(Order).where(Order.broker_id == w["broker_id"])
                    )
                )
                .scalars()
                .all()
            )
            assert orders == []
        finally:
            await set_emergency_stop(
                session, active=False, reason="test teardown", actor_user_id=None
            )
            await session.commit()


@pytest.mark.asyncio
async def test_a_paused_deployment_is_filtered_out_of_the_cycle_entirely() -> None:
    async with db_session() as session, deployment_world(session, seed={"g": BUY_CLOSES}) as w:
        dep_id = await _deploy(session, w, symbols=[w["symbols"]["g"]])
        deployment = await session.get(StrategyDeployment, dep_id)
        deployment.status = StrategyDeploymentStatus.PAUSED
        await session.commit()

        result = await run_deployment_cycle(_SF, settings=_SETTINGS)
        assert _mine(result, dep_id) is None  # not enumerated at all

        orders = (
            (await session.execute(select(Order).where(Order.broker_id == w["broker_id"])))
            .scalars()
            .all()
        )
        assert orders == []


@pytest.mark.asyncio
async def test_a_pending_approval_deployment_is_invisible_to_the_runner() -> None:
    async with db_session() as session, deployment_world(session, seed={"g": BUY_CLOSES}) as w:
        dep_id = await _deploy(session, w, symbols=[w["symbols"]["g"]], approved=False)

        result = await run_deployment_cycle(_SF, settings=_SETTINGS)
        assert _mine(result, dep_id) is None

        orders = (
            (await session.execute(select(Order).where(Order.broker_id == w["broker_id"])))
            .scalars()
            .all()
        )
        assert orders == []


@pytest.mark.asyncio
async def test_a_symbol_with_too_few_bars_is_evaluated_but_places_nothing() -> None:
    async with db_session() as session, deployment_world(session, seed={"g": [100, 101]}) as w:
        dep_id = await _deploy(session, w, symbols=[w["symbols"]["g"]])

        result = await run_deployment_cycle(_SF, settings=_SETTINGS)
        outcome = _mine(result, dep_id)
        assert outcome.status is StrategyDeploymentRunStatus.SUCCEEDED
        assert outcome.symbols_evaluated == 1
        assert outcome.signals_actionable == 0
        assert outcome.orders_submitted == 0

        signal = (
            await session.execute(
                select(SignalEvaluation).where(
                    SignalEvaluation.deployment_run_id == outcome.run_id
                )
            )
        ).scalar_one()
        assert signal.insufficient_data is True


@pytest.mark.asyncio
async def test_an_unpriceable_held_position_fails_the_cycle_visibly() -> None:
    async with db_session() as session, deployment_world(session, seed={"g": BUY_CLOSES}) as w:
        dep_id = await _deploy(session, w, symbols=[w["symbols"]["g"]])
        session.add(
            BrokerPosition(
                broker_id=w["broker_id"], symbol="UNPRICEABLEDEP.US", quantity=Decimal(5)
            )
        )
        await session.commit()

        result = await run_deployment_cycle(_SF, settings=_SETTINGS)
        outcome = _mine(result, dep_id)
        assert outcome.status is StrategyDeploymentRunStatus.FAILED
        assert "UNPRICEABLEDEP.US" in (outcome.detail or "")

        run = (
            await session.execute(
                select(StrategyDeploymentRun).where(StrategyDeploymentRun.id == outcome.run_id)
            )
        ).scalar_one()
        assert run.status is StrategyDeploymentRunStatus.FAILED
        assert run.error_detail and "fabricated" in run.error_detail


@pytest.mark.asyncio
async def test_a_utc_weekend_no_ops_the_whole_cycle() -> None:
    saturday = datetime(2026, 2, 7, 12, 0, tzinfo=UTC)
    async with db_session() as session, deployment_world(session, seed={"g": BUY_CLOSES}) as w:
        await _deploy(session, w, symbols=[w["symbols"]["g"]])

        result = await run_deployment_cycle(
            _SF,
            settings=_SETTINGS,
            market_hours_gate=MarketHoursGate(enabled=True),
            clock=lambda: saturday,
        )
        assert result.status is DeploymentCycleStatus.SKIPPED_MARKET_CLOSED
        assert result.outcomes == ()

        orders = (
            (await session.execute(select(Order).where(Order.broker_id == w["broker_id"])))
            .scalars()
            .all()
        )
        assert orders == []


@pytest.mark.asyncio
async def test_a_live_deployment_is_always_skipped_even_with_live_trading_nominally_on() -> None:
    """Phase 64, D082: the runner's refusal to place a live order is
    unconditional - it does not consult TRADING_MODE / LIVE_TRADING_ENABLED
    at all. Proven here by handing it settings where the triple gate would
    otherwise be wide open, and asserting nothing changes: still skipped,
    still zero orders, still zero broker/market-data access for this
    deployment."""
    live_settings = _SETTINGS.model_copy(
        update={"trading_mode": TradingMode.LIVE, "live_trading_enabled": True}
    )
    async with (
        db_session() as session,
        deployment_world(session, seed={"g": BUY_CLOSES}, with_live_broker=True) as w,
    ):
        good = w["symbols"]["g"]
        dep_id = await _deploy(
            session, w, symbols=[good], broker_id=w["live_broker_id"], mode="live"
        )

        result = await run_deployment_cycle(_SF, settings=live_settings)
        outcome = _mine(result, dep_id)
        assert outcome is not None
        assert outcome.status is StrategyDeploymentRunStatus.SKIPPED_LIVE_TRADING_DISABLED
        assert outcome.orders_submitted == 0
        assert outcome.symbols_evaluated == 0  # never even read a bar for this symbol

        run = (
            await session.execute(
                select(StrategyDeploymentRun).where(StrategyDeploymentRun.id == outcome.run_id)
            )
        ).scalar_one()
        assert run.status is StrategyDeploymentRunStatus.SKIPPED_LIVE_TRADING_DISABLED
        assert run.error_detail and "per-trade human" in run.error_detail

        signals = (
            await session.execute(
                select(SignalEvaluation).where(SignalEvaluation.deployment_run_id == run.id)
            )
        ).scalars().all()
        assert signals == []  # no signal evaluated either - the skip is first, full stop

        orders = (
            (await session.execute(select(Order).where(Order.broker_id == w["live_broker_id"])))
            .scalars()
            .all()
        )
        assert orders == []


# ------------------------------------------------------------------------
# Phase 66 (D084): drift detection wired into the runner's success path
# ------------------------------------------------------------------------


def _drift_backtest_run(
    *, version_id: uuid.UUID, symbol: str, win_rate_pct: Decimal
) -> BacktestRun:
    return BacktestRun(
        id=uuid.uuid4(),
        strategy_version_id=version_id,
        requested_by_user_id=None,
        symbol=symbol,
        bar_interval="1d",
        start_date=_DRIFT_BACKTEST_NOW.date(),
        end_date=_DRIFT_BACKTEST_NOW.date(),
        starting_cash=Decimal(100_000),
        status=BacktestRunStatus.SUCCEEDED,
        final_equity=Decimal(110_000),
        total_return_pct=Decimal("10.0000"),
        max_drawdown_pct=Decimal("5.0000"),
        win_rate_pct=win_rate_pct,
        num_trades=10,
    )


async def _latest_drift_check(session, deployment_id: uuid.UUID) -> StrategyDriftCheck:
    return (
        await session.execute(
            select(StrategyDriftCheck)
            .where(StrategyDriftCheck.deployment_id == deployment_id)
            .order_by(StrategyDriftCheck.created_at.desc(), StrategyDriftCheck.id.desc())
            .limit(1)
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_drift_detected_with_auto_pause_enabled_pauses_the_deployment() -> None:
    """A closed round trip (BUY then SELL) against a reference backtest with
    a far-off win rate exceeds the default 30-point threshold. With
    `strategy_drift_auto_pause_enabled=True`, the next SUCCEEDED cycle pauses
    the deployment itself, through the existing `pause_deployment()`."""
    drift_settings = _SETTINGS.model_copy(
        update={"strategy_drift_min_round_trips": 1, "strategy_drift_auto_pause_enabled": True}
    )
    async with db_session() as session, deployment_world(session, seed={"g": BUY_CLOSES}) as w:
        good = w["symbols"]["g"]
        dep_id = await _deploy(session, w, symbols=[good])

        first = await run_deployment_cycle(_SF, settings=drift_settings)
        assert _mine(first, dep_id).orders_filled == 1

        await MarketDataStore(session).upsert_bars(_weekday_bars(good, SELL_CLOSES))
        await session.commit()
        second = await run_deployment_cycle(_SF, settings=drift_settings)
        assert _mine(second, dep_id).orders_filled == 1  # closes the round trip

        deployment = await session.get(StrategyDeployment, dep_id)
        monitoring = await build_deployment_monitoring(session, deployment)
        actual_rate = monitoring.actual.win_rate_pct
        assert actual_rate is not None
        far_rate = Decimal(0) if actual_rate >= Decimal(50) else Decimal(100)
        backtest_run = _drift_backtest_run(
            version_id=w["version_id"], symbol=good, win_rate_pct=far_rate
        )
        session.add(backtest_run)
        await session.commit()

        try:
            # A third, no-op cycle (already flat, no fresh signal) still runs
            # the drift check on its SUCCEEDED path.
            third = await run_deployment_cycle(_SF, settings=drift_settings)
            outcome = _mine(third, dep_id)
            assert outcome is not None
            assert outcome.status is StrategyDeploymentRunStatus.SUCCEEDED

            await session.refresh(deployment)
            assert deployment.status is StrategyDeploymentStatus.PAUSED
            assert deployment.paused_reason and "drift detected" in deployment.paused_reason

            check = await _latest_drift_check(session, dep_id)
            assert check.status is DriftCheckStatus.DRIFT_DETECTED
            assert check.action_taken == "paused"
            assert check.actual_win_rate_pct == actual_rate
            assert check.expected_win_rate_pct == far_rate
        finally:
            await session.delete(backtest_run)
            await session.commit()


@pytest.mark.asyncio
async def test_drift_detected_with_auto_pause_disabled_leaves_the_deployment_active() -> None:
    """The same drift scenario as above, but with
    `strategy_drift_auto_pause_enabled` left at its default (`False`): the
    check is still recorded, but nothing about the deployment changes."""
    drift_settings = _SETTINGS.model_copy(
        update={"strategy_drift_min_round_trips": 1, "strategy_drift_auto_pause_enabled": False}
    )
    async with db_session() as session, deployment_world(session, seed={"g": BUY_CLOSES}) as w:
        good = w["symbols"]["g"]
        dep_id = await _deploy(session, w, symbols=[good])

        first = await run_deployment_cycle(_SF, settings=drift_settings)
        assert _mine(first, dep_id).orders_filled == 1

        await MarketDataStore(session).upsert_bars(_weekday_bars(good, SELL_CLOSES))
        await session.commit()
        second = await run_deployment_cycle(_SF, settings=drift_settings)
        assert _mine(second, dep_id).orders_filled == 1

        deployment = await session.get(StrategyDeployment, dep_id)
        monitoring = await build_deployment_monitoring(session, deployment)
        actual_rate = monitoring.actual.win_rate_pct
        assert actual_rate is not None
        far_rate = Decimal(0) if actual_rate >= Decimal(50) else Decimal(100)
        backtest_run = _drift_backtest_run(
            version_id=w["version_id"], symbol=good, win_rate_pct=far_rate
        )
        session.add(backtest_run)
        await session.commit()

        try:
            third = await run_deployment_cycle(_SF, settings=drift_settings)
            outcome = _mine(third, dep_id)
            assert outcome is not None
            assert outcome.status is StrategyDeploymentRunStatus.SUCCEEDED

            await session.refresh(deployment)
            assert deployment.status is StrategyDeploymentStatus.ACTIVE
            assert deployment.paused_reason is None

            check = await _latest_drift_check(session, dep_id)
            assert check.status is DriftCheckStatus.DRIFT_DETECTED
            assert check.action_taken == "observed_only"
        finally:
            await session.delete(backtest_run)
            await session.commit()


@pytest.mark.asyncio
async def test_too_few_round_trips_writes_insufficient_data_and_never_touches_status() -> None:
    """A deployment that has only ever bought (never sold) has zero closed
    round trips - below the default `strategy_drift_min_round_trips=10` -
    even though a reference backtest DOES exist (isolating this from the
    separate "no reference backtest" reason for `INSUFFICIENT_DATA`, which
    `tests/deployments/test_drift.py` covers directly). The cycle still
    writes a drift-check row (`action_taken == "none"`), and the
    deployment's own status is untouched."""
    async with db_session() as session, deployment_world(session, seed={"g": BUY_CLOSES}) as w:
        good = w["symbols"]["g"]
        dep_id = await _deploy(session, w, symbols=[good])
        backtest_run = _drift_backtest_run(
            version_id=w["version_id"], symbol=good, win_rate_pct=Decimal("60.0000")
        )
        session.add(backtest_run)
        await session.commit()

        try:
            result = await run_deployment_cycle(_SF, settings=_SETTINGS)
            outcome = _mine(result, dep_id)
            assert outcome is not None
            assert outcome.status is StrategyDeploymentRunStatus.SUCCEEDED

            check = await _latest_drift_check(session, dep_id)
            assert check.status is DriftCheckStatus.INSUFFICIENT_DATA
            assert check.action_taken == "none"
            assert check.actual_win_rate_pct is None
            assert check.expected_win_rate_pct is None
            assert check.num_round_trips == 0

            deployment = await session.get(StrategyDeployment, dep_id)
            assert deployment.status is StrategyDeploymentStatus.ACTIVE
            assert deployment.paused_reason is None
        finally:
            await session.delete(backtest_run)
            await session.commit()
