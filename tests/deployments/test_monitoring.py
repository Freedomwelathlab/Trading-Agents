"""Tests for strategy-deployment monitoring (Phase 65, D083).

Two layers, like the rest of `tests/deployments/`:
- Pure unit tests of `_build_round_trips` against hand-built `Order`/`Fill`
  ORM instances - no database, no session, just the in-memory walking logic.
- Integration tests of `build_deployment_monitoring` against real Postgres,
  reusing `deployment_world` / `run_deployment_cycle` exactly as
  `tests/deployments/test_runner.py` does, so the round trips under test come
  from a real filled paper order, not a fabricated one.
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
    Fill,
    Order,
    OrderStatus,
    StrategyDeployment,
)
from apps.api.app.deployments.monitoring import (
    _build_round_trips,
    build_deployment_monitoring,
)
from apps.api.app.deployments.runner import run_deployment_cycle
from apps.api.app.deployments.service import approve_deployment, create_deployment
from apps.api.app.marketdata.store import MarketDataStore
from apps.api.app.risk.models import Side
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


def _order(*, symbol: str, side: Side) -> Order:
    return Order(
        id=uuid.uuid4(),
        broker_id=uuid.uuid4(),
        symbol=symbol,
        side=side,
        quantity=Decimal(10),
        estimated_price=Decimal(100),
        status=OrderStatus.FILLED,
    )


def _fill(order: Order, *, quantity: Decimal, price: Decimal, at: datetime) -> Fill:
    return Fill(
        id=uuid.uuid4(), order_id=order.id, quantity=quantity, fill_price=price, filled_at=at
    )


# ------------------------------------------------------------------------
# Pure unit tests of _build_round_trips
# ------------------------------------------------------------------------


def test_a_clean_buy_then_sell_produces_one_winning_round_trip() -> None:
    buy = _order(symbol="AAA", side=Side.BUY)
    sell = _order(symbol="AAA", side=Side.SELL)
    entered = NOW
    exited = NOW.replace(day=2)
    pairs = [
        (buy, _fill(buy, quantity=Decimal(10), price=Decimal(100), at=entered)),
        (sell, _fill(sell, quantity=Decimal(10), price=Decimal(110), at=exited)),
    ]

    round_trips, open_positions = _build_round_trips(pairs)

    assert open_positions == {}
    assert len(round_trips) == 1
    rt = round_trips[0]
    assert rt.symbol == "AAA"
    assert rt.quantity == Decimal(10)
    assert rt.entry_price == Decimal(100)
    assert rt.entered_at == entered
    assert rt.exit_price == Decimal(110)
    assert rt.exited_at == exited
    assert rt.realized_pnl == Decimal(100)  # (110-100)*10
    assert rt.return_pct == Decimal(10)  # (110-100)/100*100
    assert rt.realized_pnl > 0


def test_a_clean_buy_then_sell_produces_one_losing_round_trip() -> None:
    buy = _order(symbol="BBB", side=Side.BUY)
    sell = _order(symbol="BBB", side=Side.SELL)
    pairs = [
        (buy, _fill(buy, quantity=Decimal(5), price=Decimal(200), at=NOW)),
        (sell, _fill(sell, quantity=Decimal(5), price=Decimal(180), at=NOW.replace(day=3))),
    ]

    round_trips, open_positions = _build_round_trips(pairs)

    assert open_positions == {}
    assert len(round_trips) == 1
    rt = round_trips[0]
    assert rt.realized_pnl == Decimal(-100)  # (180-200)*5
    assert rt.return_pct == Decimal(-10)  # (180-200)/200*100
    assert rt.realized_pnl < 0


def test_an_unclosed_buy_leaves_an_open_position_and_zero_round_trips() -> None:
    buy = _order(symbol="CCC", side=Side.BUY)
    pairs = [(buy, _fill(buy, quantity=Decimal(7), price=Decimal(50), at=NOW))]

    round_trips, open_lots = _build_round_trips(pairs)

    assert round_trips == []
    assert set(open_lots) == {"CCC"}
    # Since D087 the lot carries its real entry fill, not just a quantity.
    assert open_lots["CCC"].quantity == Decimal(7)
    assert open_lots["CCC"].entry_price == Decimal(50)
    assert open_lots["CCC"].cost_basis == Decimal(350)


def test_two_interleaved_symbols_do_not_cross_contaminate_lots() -> None:
    buy_a = _order(symbol="AAA", side=Side.BUY)
    buy_b = _order(symbol="BBB", side=Side.BUY)
    sell_a = _order(symbol="AAA", side=Side.SELL)
    sell_b = _order(symbol="BBB", side=Side.SELL)
    pairs = [
        (buy_a, _fill(buy_a, quantity=Decimal(10), price=Decimal(100), at=NOW)),
        (buy_b, _fill(buy_b, quantity=Decimal(20), price=Decimal(50), at=NOW)),
        (sell_a, _fill(sell_a, quantity=Decimal(10), price=Decimal(120), at=NOW.replace(day=2))),
        (sell_b, _fill(sell_b, quantity=Decimal(20), price=Decimal(40), at=NOW.replace(day=2))),
    ]

    round_trips, open_positions = _build_round_trips(pairs)

    assert open_positions == {}
    by_symbol = {rt.symbol: rt for rt in round_trips}
    assert set(by_symbol) == {"AAA", "BBB"}
    assert by_symbol["AAA"].entry_price == Decimal(100)
    assert by_symbol["AAA"].exit_price == Decimal(120)
    assert by_symbol["BBB"].entry_price == Decimal(50)
    assert by_symbol["BBB"].exit_price == Decimal(40)


def test_a_buy_while_already_open_is_skipped_not_merged() -> None:
    buy1 = _order(symbol="AAA", side=Side.BUY)
    buy2 = _order(symbol="AAA", side=Side.BUY)
    pairs = [
        (buy1, _fill(buy1, quantity=Decimal(10), price=Decimal(100), at=NOW)),
        (buy2, _fill(buy2, quantity=Decimal(5), price=Decimal(200), at=NOW.replace(day=2))),
    ]

    round_trips, open_lots = _build_round_trips(pairs)

    assert round_trips == []
    # The original lot survives untouched - never averaged with the second
    # buy. Since D087 this is provable on the PRICE as well as the quantity:
    # an averaged lot would read 15 @ 133.33, and the cost basis a live
    # capital ceiling is measured against would be wrong in the direction
    # that lets the robot commit more than it should.
    assert set(open_lots) == {"AAA"}
    assert open_lots["AAA"].quantity == Decimal(10)
    assert open_lots["AAA"].entry_price == Decimal(100)
    assert open_lots["AAA"].entered_at == NOW


def test_a_sell_with_no_open_lot_is_skipped() -> None:
    sell = _order(symbol="AAA", side=Side.SELL)
    pairs = [(sell, _fill(sell, quantity=Decimal(10), price=Decimal(100), at=NOW))]

    round_trips, open_positions = _build_round_trips(pairs)

    assert round_trips == []
    assert open_positions == {}


# ------------------------------------------------------------------------
# Integration tests of build_deployment_monitoring against real Postgres
# ------------------------------------------------------------------------


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


@pytest.mark.asyncio
async def test_buy_then_sell_cycle_reports_one_closed_round_trip() -> None:
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
        result = await build_deployment_monitoring(session, deployment)

        assert result.deployment_id == dep_id
        assert result.actual.num_round_trips == 1
        assert result.actual.open_positions == {}
        rt = result.actual.round_trips[0]
        assert rt.symbol == good
        # BUY_CLOSES ends higher than it starts trading and SELL_CLOSES ends
        # lower - the exact sign depends on engine sizing/fill price, but the
        # realized pnl must be consistent with entry/exit prices.
        expected_sign = 1 if rt.exit_price > rt.entry_price else -1
        assert (rt.realized_pnl > 0) == (expected_sign == 1)
        assert result.actual.win_rate_pct in (Decimal("0.0000"), Decimal("100.0000"))
        assert result.expected.status == "no_reference_backtest"
        assert result.expected.reference_backtest_run_id is None
        assert result.expected.total_return_pct is None
        assert result.expected.win_rate_pct is None
        assert result.expected.num_trades is None
        assert result.expected.symbol is None


@pytest.mark.asyncio
async def test_a_filled_buy_with_no_sell_reports_zero_round_trips_and_open_position() -> None:
    async with db_session() as session, deployment_world(session, seed={"g": BUY_CLOSES}) as w:
        good = w["symbols"]["g"]
        dep_id = await _deploy(session, w, symbols=[good])

        result_cycle = await run_deployment_cycle(_SF, settings=_SETTINGS)
        assert any(
            o.deployment_id == dep_id and o.orders_filled == 1 for o in result_cycle.outcomes
        )

        deployment = await session.get(StrategyDeployment, dep_id)
        result = await build_deployment_monitoring(session, deployment)

        assert result.actual.num_round_trips == 0
        assert result.actual.win_rate_pct is None
        assert result.actual.avg_return_pct is None
        assert result.actual.total_realized_pnl == Decimal(0)
        assert good in result.actual.open_positions
        assert result.actual.open_positions[good] > 0


@pytest.mark.asyncio
async def test_no_reference_backtest_leaves_every_expected_field_none() -> None:
    async with db_session() as session, deployment_world(session, seed={}) as w:
        dep = await create_deployment(
            session,
            strategy_version_id=w["version_id"],
            broker_id=w["broker_id"],
            symbols=["ZZZ.US"],
            bar_interval="1d",
            mode="paper",
            requested_by_user_id=None,
        )
        await approve_deployment(dep, approved_by_user_id=None)
        await session.commit()

        result = await build_deployment_monitoring(session, dep)

        assert result.expected.status == "no_reference_backtest"
        assert result.expected.reference_backtest_run_id is None
        assert result.expected.symbol is None
        assert result.expected.total_return_pct is None
        assert result.expected.max_drawdown_pct is None
        assert result.expected.win_rate_pct is None
        assert result.expected.num_trades is None


@pytest.mark.asyncio
async def test_a_succeeded_backtest_run_populates_expected_verbatim() -> None:
    async with db_session() as session, deployment_world(session, seed={}) as w:
        dep = await create_deployment(
            session,
            strategy_version_id=w["version_id"],
            broker_id=w["broker_id"],
            symbols=["ZZZ.US"],
            bar_interval="1d",
            mode="paper",
            requested_by_user_id=None,
        )
        await approve_deployment(dep, approved_by_user_id=None)

        backtest_run = BacktestRun(
            id=uuid.uuid4(),
            strategy_version_id=w["version_id"],
            requested_by_user_id=None,
            symbol="ZZZ.US",
            bar_interval="1d",
            start_date=NOW.date(),
            end_date=NOW.date(),
            starting_cash=Decimal(100_000),
            status=BacktestRunStatus.SUCCEEDED,
            final_equity=Decimal(110_000),
            total_return_pct=Decimal("10.0000"),
            max_drawdown_pct=Decimal("5.0000"),
            win_rate_pct=Decimal("60.0000"),
            num_trades=4,
        )
        session.add(backtest_run)
        await session.commit()

        try:
            result = await build_deployment_monitoring(session, dep)

            assert result.expected.status == "available"
            assert result.expected.reference_backtest_run_id == backtest_run.id
            assert result.expected.symbol == "ZZZ.US"
            assert result.expected.total_return_pct == Decimal("10.0000")
            assert result.expected.max_drawdown_pct == Decimal("5.0000")
            assert result.expected.win_rate_pct == Decimal("60.0000")
            assert result.expected.num_trades == 4
        finally:
            await session.delete(backtest_run)
            await session.commit()
