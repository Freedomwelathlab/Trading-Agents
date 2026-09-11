"""Tests for drift detection (Phase 66, D084), against real Postgres.

Reuses `deployment_world` / `run_deployment_cycle` from
`tests/deployments/conftest.py` exactly as `tests/deployments/test_monitoring.py`
does, so the round trips `evaluate_deployment_drift` judges come from real
filled paper orders and a real, hand-built reference `BacktestRun` row -
never a fabricated result.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.core.config import get_settings
from apps.api.app.db.base import get_session_factory
from apps.api.app.db.models import (
    BacktestRun,
    BacktestRunStatus,
    DriftCheckStatus,
    StrategyDeployment,
)
from apps.api.app.deployments.drift import evaluate_deployment_drift
from apps.api.app.deployments.monitoring import build_deployment_monitoring
from apps.api.app.deployments.runner import run_deployment_cycle
from apps.api.app.deployments.service import approve_deployment, create_deployment
from apps.api.app.marketdata.store import MarketDataStore
from tests.deployments.conftest import (
    BUY_CLOSES,
    SELL_CLOSES,
    _weekday_bars,
    db_session,
    deployment_world,
)

_SF = get_session_factory()
_SETTINGS = get_settings()

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


async def _deploy(session, world, *, symbols) -> uuid.UUID:
    dep = await create_deployment(
        session,
        strategy_version_id=world["version_id"],
        broker_id=world["broker_id"],
        symbols=symbols,
        bar_interval="1d",
        mode="paper",
        requested_by_user_id=None,
    )
    await approve_deployment(dep, approved_by_user_id=None)
    await session.commit()
    return dep.id


def _backtest_run(*, version_id: uuid.UUID, symbol: str, win_rate_pct: Decimal) -> BacktestRun:
    return BacktestRun(
        id=uuid.uuid4(),
        strategy_version_id=version_id,
        requested_by_user_id=None,
        symbol=symbol,
        bar_interval="1d",
        start_date=NOW.date(),
        end_date=NOW.date(),
        starting_cash=Decimal(100_000),
        status=BacktestRunStatus.SUCCEEDED,
        final_equity=Decimal(110_000),
        total_return_pct=Decimal("10.0000"),
        max_drawdown_pct=Decimal("5.0000"),
        win_rate_pct=win_rate_pct,
        num_trades=10,
    )


@pytest.mark.asyncio
async def test_too_few_round_trips_is_insufficient_data() -> None:
    """A deployment that has only ever bought (never sold) has zero closed
    round trips - below any positive `min_round_trips` threshold."""
    async with db_session() as session, deployment_world(session, seed={"g": BUY_CLOSES}) as w:
        good = w["symbols"]["g"]
        dep_id = await _deploy(session, w, symbols=[good])

        outcome = await run_deployment_cycle(_SF, settings=_SETTINGS)
        assert any(o.deployment_id == dep_id and o.orders_filled == 1 for o in outcome.outcomes)

        backtest_run = _backtest_run(
            version_id=w["version_id"], symbol=good, win_rate_pct=Decimal("60.0000")
        )
        session.add(backtest_run)
        await session.commit()
        try:
            deployment = await session.get(StrategyDeployment, dep_id)
            result = await evaluate_deployment_drift(
                session,
                deployment,
                min_round_trips=10,
                max_win_rate_deviation_pct=Decimal("30"),
            )
            assert result.status is DriftCheckStatus.INSUFFICIENT_DATA
            assert result.actual_win_rate_pct is None
            assert result.expected_win_rate_pct is None
            assert result.win_rate_deviation_pct is None
            assert result.num_round_trips == 0
            assert "closed round trip" in result.detail
        finally:
            await session.delete(backtest_run)
            await session.commit()


@pytest.mark.asyncio
async def test_no_reference_backtest_is_insufficient_data() -> None:
    """A buy-then-sell cycle produces one closed round trip, but with no
    `BacktestRun` row at all there is nothing honest to compare it to."""
    async with db_session() as session, deployment_world(session, seed={"g": BUY_CLOSES}) as w:
        good = w["symbols"]["g"]
        dep_id = await _deploy(session, w, symbols=[good])

        first = await run_deployment_cycle(_SF, settings=_SETTINGS)
        assert any(o.deployment_id == dep_id and o.orders_filled == 1 for o in first.outcomes)

        await MarketDataStore(session).upsert_bars(_weekday_bars(good, SELL_CLOSES))
        await session.commit()
        second = await run_deployment_cycle(_SF, settings=_SETTINGS)
        assert any(o.deployment_id == dep_id and o.orders_filled == 1 for o in second.outcomes)

        deployment = await session.get(StrategyDeployment, dep_id)
        result = await evaluate_deployment_drift(
            session,
            deployment,
            min_round_trips=1,
            max_win_rate_deviation_pct=Decimal("30"),
        )
        assert result.status is DriftCheckStatus.INSUFFICIENT_DATA
        assert result.actual_win_rate_pct is None
        assert result.expected_win_rate_pct is None
        assert result.win_rate_deviation_pct is None
        assert "no reference backtest" in result.detail


@pytest.mark.asyncio
async def test_close_win_rates_report_no_drift() -> None:
    """One closed round trip is either a 0% or 100% actual win rate (see
    `deployments/monitoring.py`'s own test comment) - pick the reference
    backtest's win rate to be close to whichever one this run actually
    produces, well within the default 30-point threshold."""
    async with db_session() as session, deployment_world(session, seed={"g": BUY_CLOSES}) as w:
        good = w["symbols"]["g"]
        dep_id = await _deploy(session, w, symbols=[good])

        first = await run_deployment_cycle(_SF, settings=_SETTINGS)
        assert any(o.deployment_id == dep_id and o.orders_filled == 1 for o in first.outcomes)
        await MarketDataStore(session).upsert_bars(_weekday_bars(good, SELL_CLOSES))
        await session.commit()
        second = await run_deployment_cycle(_SF, settings=_SETTINGS)
        assert any(o.deployment_id == dep_id and o.orders_filled == 1 for o in second.outcomes)

        deployment = await session.get(StrategyDeployment, dep_id)
        monitoring = await build_deployment_monitoring(session, deployment)
        actual_rate = monitoring.actual.win_rate_pct
        assert actual_rate is not None

        # Reference backtest's win rate 10 points away from actual - within
        # the default 30-point threshold.
        if actual_rate <= Decimal(90):
            close_rate = actual_rate + Decimal(10)
        else:
            close_rate = actual_rate - Decimal(10)
        backtest_run = _backtest_run(
            version_id=w["version_id"], symbol=good, win_rate_pct=close_rate
        )
        session.add(backtest_run)
        await session.commit()
        try:
            result = await evaluate_deployment_drift(
                session,
                deployment,
                min_round_trips=1,
                max_win_rate_deviation_pct=Decimal("30"),
            )
            assert result.status is DriftCheckStatus.NO_DRIFT
            assert result.actual_win_rate_pct == actual_rate
            assert result.expected_win_rate_pct == close_rate
            assert result.win_rate_deviation_pct == Decimal(10)
            assert result.num_round_trips == 1
            assert "within the configured maximum" in result.detail
        finally:
            await session.delete(backtest_run)
            await session.commit()


@pytest.mark.asyncio
async def test_far_apart_win_rates_report_drift_detected() -> None:
    """The reference backtest's win rate is set far enough from the actual
    (real) win rate to exceed the configured threshold."""
    async with db_session() as session, deployment_world(session, seed={"g": BUY_CLOSES}) as w:
        good = w["symbols"]["g"]
        dep_id = await _deploy(session, w, symbols=[good])

        first = await run_deployment_cycle(_SF, settings=_SETTINGS)
        assert any(o.deployment_id == dep_id and o.orders_filled == 1 for o in first.outcomes)
        await MarketDataStore(session).upsert_bars(_weekday_bars(good, SELL_CLOSES))
        await session.commit()
        second = await run_deployment_cycle(_SF, settings=_SETTINGS)
        assert any(o.deployment_id == dep_id and o.orders_filled == 1 for o in second.outcomes)

        deployment = await session.get(StrategyDeployment, dep_id)
        monitoring = await build_deployment_monitoring(session, deployment)
        actual_rate = monitoring.actual.win_rate_pct
        assert actual_rate is not None

        far_rate = Decimal(0) if actual_rate >= Decimal(50) else Decimal(100)
        backtest_run = _backtest_run(version_id=w["version_id"], symbol=good, win_rate_pct=far_rate)
        session.add(backtest_run)
        await session.commit()
        try:
            result = await evaluate_deployment_drift(
                session,
                deployment,
                min_round_trips=1,
                max_win_rate_deviation_pct=Decimal("30"),
            )
            assert result.status is DriftCheckStatus.DRIFT_DETECTED
            assert result.actual_win_rate_pct == actual_rate
            assert result.expected_win_rate_pct == far_rate
            assert result.win_rate_deviation_pct == Decimal(100)
            assert result.num_round_trips == 1
            assert "exceeding the configured maximum" in result.detail
        finally:
            await session.delete(backtest_run)
            await session.commit()
