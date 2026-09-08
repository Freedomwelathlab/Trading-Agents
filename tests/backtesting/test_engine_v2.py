"""The Phase 55 persisted backtest engine's own mechanics: position sizing,
the RISK -> PORTFOLIO -> BROKER replay over real dated bars, and what
actually lands in `backtest_runs` / `backtest_equity_points` /
`backtest_trades`.

DB-backed, against a real Postgres instance, because persistence is half of
what this module does - there is no meaningful unit-level version of "the
run row reached SUCCEEDED with these five equity points".

**`generate_signals` is patched out in every test here, deliberately.** This
module is not the signal evaluator's test (tests/backtesting/test_executor.py
is); mixing the two would mean a change in the evaluator's crossing
semantics could silently change what these tests believe about SIZING or
about the replay loop. Injecting a hand-crafted signal list instead makes
every number below hand-derivable from the bars and the limits alone -
which is what lets each expected value be pinned exactly, the way
tests/backtesting/test_engine_portfolio_manager.py pins v1's.

Everything else is REAL: the real Risk Engine, the real PaperBrokerAdapter
fill math, the real metrics module, and real rows in a real database.
"""

import contextlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select

from apps.api.app.backtesting.engine_v2 import _desired_quantity, run_strategy_backtest
from apps.api.app.backtesting.strategy import Signal
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    BacktestEquityPoint,
    BacktestRun,
    BacktestRunStatus,
    BacktestTrade,
    Strategy,
    StrategyVersion,
    StrategyVersionStatus,
)
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.risk.models import RiskLimits
from apps.api.app.strategies.service import compute_definition_hash

TEST_SYMBOL = "TESTENGINEV2.US"

# SMA(2) => warmup_bar_count = max(period) + 1 = 3 bars before start_date.
DEFINITION_ALL_IN = {
    "indicators": [{"id": "sma_2", "type": "sma", "period": 2}],
    "entry_rule": {"op": "crosses_above", "left": "close", "right": "sma_2"},
    "exit_rule": {"op": "crosses_below", "left": "close", "right": "sma_2"},
    "position_sizing": {"type": "all_in"},
}

START_DATE = date(2026, 3, 2)
END_DATE = date(2026, 3, 6)

WARMUP_DATES = [date(2026, 2, 25), date(2026, 2, 26), date(2026, 2, 27)]
WINDOW_DATES = [
    date(2026, 3, 2),
    date(2026, 3, 3),
    date(2026, 3, 4),
    date(2026, 3, 5),
    date(2026, 3, 6),
]
WINDOW_CLOSES = [Decimal(100), Decimal(110), Decimal(120), Decimal(90), Decimal(95)]

# One BUY on the window's first bar, one SELL on its fourth. Everything the
# warmup covers is HOLD, exactly as a real evaluator would produce while an
# indicator is still inside its own warmup.
BUY_THEN_SELL_SIGNALS = [Signal.HOLD] * 3 + [
    Signal.BUY,
    Signal.HOLD,
    Signal.HOLD,
    Signal.SELL,
    Signal.HOLD,
]


def _bar(day: date, close: Decimal) -> Bar:
    return Bar(
        symbol=TEST_SYMBOL,
        bar_interval="1d",
        ts=datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
        close=close,
        source="test-engine-v2",
    )


class FakeBarProvider:
    """A HistoricalBarProvider that answers from a fixed list, filtered by
    the requested range exactly as MarketDataStore does - so a test that
    withholds warmup history really does withhold it rather than relying on
    the engine not asking."""

    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars
        self.calls: list[tuple] = []

    async def get_bars(self, symbol, *, bar_interval, start_date, end_date):
        self.calls.append((symbol, bar_interval, start_date, end_date))
        return [
            bar
            for bar in self._bars
            if bar.symbol == symbol
            and bar.bar_interval == bar_interval
            and start_date <= bar.ts.astimezone(UTC).date() <= end_date
        ]


def _full_bars() -> list[Bar]:
    warmup = [_bar(day, Decimal(100)) for day in WARMUP_DATES]
    window = [_bar(day, close) for day, close in zip(WINDOW_DATES, WINDOW_CLOSES, strict=True)]
    return warmup + window


def _risk_limits() -> RiskLimits:
    """A 30% single-position cap, everything else deliberately loose.

    30% is chosen so that `all_in` is capped by the RISK ENGINE (as it is in
    v1) while `fixed_fraction`/`fixed_notional` below stay comfortably
    underneath it - which is what makes the three sizing types produce three
    different, individually attributable fill quantities rather than three
    identical risk-capped ones.
    """
    return RiskLimits(
        max_position_pct_of_equity=Decimal("0.30"),
        max_portfolio_exposure_pct_of_equity=Decimal(1),
        max_risk_pct_of_equity_per_trade=Decimal("0.01"),
        require_stop_price=False,
        max_market_data_age_seconds=86_400,
        duplicate_order_window_seconds=5,
    )


@contextlib.asynccontextmanager
async def db_session():
    gen = get_session()
    session = await anext(gen)
    try:
        yield session
    finally:
        await gen.aclose()


@contextlib.asynccontextmanager
async def validated_version(session, definition: dict):
    """A VALIDATED StrategyVersion with no owner - `strategies.owner_user_id`
    and `strategy_versions.created_by_user_id` are both nullable, so these
    engine-level tests need no user or role fixture at all. Ownership is the
    ROUTE's concern and is tested in tests/api/test_strategy_backtests.py."""
    strategy_id = uuid.uuid4()
    version_id = uuid.uuid4()
    session.add(
        Strategy(id=strategy_id, owner_user_id=None, name=f"engine-v2-{strategy_id}")
    )
    session.add(
        StrategyVersion(
            id=version_id,
            strategy_id=strategy_id,
            version_number=1,
            definition=definition,
            definition_hash=compute_definition_hash(definition),
            status=StrategyVersionStatus.VALIDATED,
            validated_at=datetime.now(UTC),
        )
    )
    await session.commit()
    version = (
        await session.execute(select(StrategyVersion).where(StrategyVersion.id == version_id))
    ).scalar_one()
    try:
        yield version
    finally:
        await session.rollback()
        # backtest_runs must go BEFORE the version: the FK is ON DELETE
        # RESTRICT, which is the whole point of that column.
        run_ids = select(BacktestRun.id).where(BacktestRun.strategy_version_id == version_id)
        await session.execute(
            delete(BacktestTrade).where(BacktestTrade.backtest_run_id.in_(run_ids))
        )
        await session.execute(
            delete(BacktestEquityPoint).where(BacktestEquityPoint.backtest_run_id.in_(run_ids))
        )
        await session.execute(
            delete(BacktestRun).where(BacktestRun.strategy_version_id == version_id)
        )
        await session.execute(delete(Strategy).where(Strategy.id == strategy_id))
        await session.commit()


async def _run(session, version, *, bars, signals, starting_cash=Decimal(10_000)):
    with patch(
        "apps.api.app.backtesting.engine_v2.generate_signals", return_value=signals
    ) as fake:
        run = await run_strategy_backtest(
            session=session,
            strategy_version=version,
            symbol=TEST_SYMBOL,
            bar_interval="1d",
            start_date=START_DATE,
            end_date=END_DATE,
            starting_cash=starting_cash,
            bar_provider=FakeBarProvider(bars),
            risk_limits=_risk_limits(),
            portfolio_limits=None,
            requested_by_user_id=None,
        )
    return run, fake


async def _equity_points(session, run_id):
    return (
        (
            await session.execute(
                select(BacktestEquityPoint)
                .where(BacktestEquityPoint.backtest_run_id == run_id)
                .order_by(BacktestEquityPoint.date.asc())
            )
        )
        .scalars()
        .all()
    )


async def _trades(session, run_id):
    return (
        (
            await session.execute(
                select(BacktestTrade).where(BacktestTrade.backtest_run_id == run_id)
            )
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------------
# Position sizing
# --------------------------------------------------------------------------


def test_each_sizing_type_produces_its_own_hand_computed_quantity() -> None:
    """The three sizing types against the SAME account, so the differences
    are attributable to the sizing rule alone.

    Account: cash 7,000, equity 10,000 (so 3,000 is already in open
    positions), price 100.
      all_in         -> cash/price          = 7000/100        = 70
      fixed_fraction -> equity*0.10/price   = 10000*0.10/100  = 10
      fixed_notional -> min(cash,2500)/price= 2500/100        = 25
    `fixed_fraction` is against EQUITY and `all_in` against CASH, which is
    exactly why the first two differ here.
    """
    cash, equity, price = Decimal(7_000), Decimal(10_000), Decimal(100)

    assert _desired_quantity(
        sizing={"type": "all_in"}, cash=cash, equity=equity, price=price
    ) == Decimal(70)
    assert _desired_quantity(
        sizing={"type": "fixed_fraction", "fraction": 0.10},
        cash=cash,
        equity=equity,
        price=price,
    ) == Decimal(10)
    assert _desired_quantity(
        sizing={"type": "fixed_notional", "amount": 2500},
        cash=cash,
        equity=equity,
        price=price,
    ) == Decimal(25)


def test_sizing_floors_to_whole_shares_and_a_notional_larger_than_cash_is_capped() -> None:
    """Two properties in one place because they share the same inputs.

    Flooring: 999/100 = 9.99 shares -> 9, never 10 (which the account could
    not pay for) and never 9.99 (this system models no fractional share).
    Capping: a 50,000 fixed notional against 999 cash proposes
    min(999, 50000)/100 = 9.99 -> 9, not 500.
    """
    assert _desired_quantity(
        sizing={"type": "all_in"},
        cash=Decimal(999),
        equity=Decimal(999),
        price=Decimal(100),
    ) == Decimal(9)
    assert _desired_quantity(
        sizing={"type": "fixed_notional", "amount": 50_000},
        cash=Decimal(999),
        equity=Decimal(999),
        price=Decimal(100),
    ) == Decimal(9)


def test_a_sizing_that_floors_to_zero_is_a_real_answer_not_an_error() -> None:
    """A 1% fraction of a 10,000 account at a 500 price is 0.2 shares ->
    0. That is a legitimate "no trade", not a failure, and the replay loop's
    `if quantity > 0` guard is what turns it into one."""
    assert _desired_quantity(
        sizing={"type": "fixed_fraction", "fraction": 0.01},
        cash=Decimal(10_000),
        equity=Decimal(10_000),
        price=Decimal(500),
    ) == Decimal(0)


# --------------------------------------------------------------------------
# The replay + persistence
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_successful_run_persists_the_hand_computed_curve_metrics_and_trade() -> None:
    """One full run, every number derived by hand.

    Signals: BUY on 2026-03-02 (close 100), SELL on 2026-03-05 (close 90).
    Sizing is `all_in`, so the proposal is floor(10,000/100) = 100 shares.
    The Risk Engine blocks that on EXCEEDS_MAX_POSITION_SIZE (30% of 10,000
    = 3,000 notional) reporting max_quantity_allowed = floor(3000/100) = 30;
    the engine retries once at 30 (3,000 notional) and it is approved.
      Fill: cash 10,000 - 3,000 = 7,000, holding 30.
      03-02 equity = 7,000 + 30*100 = 10,000
      03-03 equity = 7,000 + 30*110 = 10,300
      03-04 equity = 7,000 + 30*120 = 10,600
      03-05 sells all 30 at 90 -> cash 7,000 + 2,700 = 9,700; equity = 9,700
      03-06 equity = 9,700 (flat, no position)
    final_equity      = 9,700
    total_return_pct  = (9700 - 10000)/10000*100 = -3
    max_drawdown_pct  = (10600 - 9700)/10600*100 = 8.490566... -> 8.4906
                        (NUMERIC(10,4) rounds on write; the row is re-read)
    win_rate_pct      = 0 (the single round trip lost)
    num_trades        = 1
    trade             = buy 30 @ 100 on 03-02, out @ 90 on 03-05, -10%
    """
    async with db_session() as session:
        async with validated_version(session, DEFINITION_ALL_IN) as version:
            run, fake = await _run(
                session, version, bars=_full_bars(), signals=BUY_THEN_SELL_SIGNALS
            )

            # The evaluator was handed warmup + window together (8 bars),
            # even though only the 5 window bars are ever reported.
            assert len(fake.call_args.args[0]) == 8

            assert run.status is BacktestRunStatus.SUCCEEDED
            assert run.error_detail is None
            assert run.completed_at is not None
            assert run.final_equity == Decimal(9_700)
            assert run.total_return_pct == Decimal(-3)
            assert run.max_drawdown_pct == Decimal("8.4906")
            assert run.win_rate_pct == Decimal(0)
            assert run.num_trades == 1

            points = await _equity_points(session, run.id)
            assert [p.date for p in points] == WINDOW_DATES
            assert [p.equity for p in points] == [
                Decimal(10_000),
                Decimal(10_300),
                Decimal(10_600),
                Decimal(9_700),
                Decimal(9_700),
            ]

            trades = await _trades(session, run.id)
            assert len(trades) == 1
            trade = trades[0]
            assert trade.side == "buy"
            assert trade.entry_date == date(2026, 3, 2)
            assert trade.entry_price == Decimal(100)
            assert trade.exit_date == date(2026, 3, 5)
            assert trade.exit_price == Decimal(90)
            assert trade.quantity == Decimal(30)
            assert trade.return_pct == Decimal(-10)


@pytest.mark.asyncio
async def test_fixed_fraction_and_fixed_notional_change_what_actually_fills() -> None:
    """The same bars and the same signals under two other sizing rules, to
    show sizing reaches the broker rather than only the proposal.

    fixed_fraction 0.10: 10,000 * 0.10 / 100 = 10 shares, well under the
    30% risk cap (30), so 10 is what fills.
      cash 10,000 - 1,000 = 9,000; sell 10 @ 90 -> 9,900. Final 9,900.
    fixed_notional 2,500: min(10,000, 2,500)/100 = 25 shares, also under
    the cap.
      cash 10,000 - 2,500 = 7,500; sell 25 @ 90 -> 9,750. Final 9,750.
    """
    fraction_definition = {
        **DEFINITION_ALL_IN,
        "position_sizing": {"type": "fixed_fraction", "fraction": 0.10},
    }
    notional_definition = {
        **DEFINITION_ALL_IN,
        "position_sizing": {"type": "fixed_notional", "amount": 2500},
    }

    async with db_session() as session:
        async with validated_version(session, fraction_definition) as version:
            run, _ = await _run(
                session, version, bars=_full_bars(), signals=BUY_THEN_SELL_SIGNALS
            )
            trades = await _trades(session, run.id)
            assert run.status is BacktestRunStatus.SUCCEEDED
            assert [t.quantity for t in trades] == [Decimal(10)]
            assert run.final_equity == Decimal(9_900)

        async with validated_version(session, notional_definition) as version:
            run, _ = await _run(
                session, version, bars=_full_bars(), signals=BUY_THEN_SELL_SIGNALS
            )
            trades = await _trades(session, run.id)
            assert run.status is BacktestRunStatus.SUCCEEDED
            assert [t.quantity for t in trades] == [Decimal(25)]
            assert run.final_equity == Decimal(9_750)


@pytest.mark.asyncio
async def test_a_sell_signal_while_flat_is_a_no_op_not_a_short() -> None:
    """Mirrors v1's behaviour exactly (engine.py only calls `_attempt_trade`
    with side=SELL under a `held > 0` guard): a SELL with no position does
    nothing at all - no fill, no trade row, no error - and the run completes
    normally with a flat curve at starting cash.

    Every window bar here carries SELL, so if a SELL while flat could reach
    the broker this run would attempt five of them."""
    async with db_session() as session:
        async with validated_version(session, DEFINITION_ALL_IN) as version:
            run, _ = await _run(
                session,
                version,
                bars=_full_bars(),
                signals=[Signal.HOLD] * 3 + [Signal.SELL] * 5,
            )

            assert run.status is BacktestRunStatus.SUCCEEDED
            assert run.num_trades == 0
            assert run.final_equity == Decimal(10_000)
            assert run.total_return_pct == Decimal(0)
            assert run.max_drawdown_pct == Decimal(0)
            # 0, not "undefined" and not fabricated - there is nothing to grade.
            assert run.win_rate_pct == Decimal(0)
            assert await _trades(session, run.id) == []
            assert [p.equity for p in await _equity_points(session, run.id)] == [
                Decimal(10_000)
            ] * 5


@pytest.mark.asyncio
async def test_insufficient_warmup_history_is_a_persisted_failed_run_not_an_exception() -> None:
    """The deliberate difference from v1. `run_backtest` raises
    `InsufficientHistoryError` for its route to translate into a 400,
    because it has nothing to persist either way. This engine already
    created a row before doing any work, so it FINISHES that row: FAILED,
    with the real error, and hands it back.

    SMA(2) needs 3 warmup bars; only 2 are supplied before start_date.
    """
    starved = [_bar(day, Decimal(100)) for day in WARMUP_DATES[1:]] + [
        _bar(day, close) for day, close in zip(WINDOW_DATES, WINDOW_CLOSES, strict=True)
    ]

    async with db_session() as session:
        async with validated_version(session, DEFINITION_ALL_IN) as version:
            run, _ = await _run(session, version, bars=starved, signals=BUY_THEN_SELL_SIGNALS)

            assert run.status is BacktestRunStatus.FAILED
            assert run.completed_at is not None
            assert run.error_detail is not None
            assert "3 1d bar(s) of indicator warmup" in run.error_detail
            assert "only 2 were found" in run.error_detail

            # Every metric is NULL, never 0 - a failed run computed none of
            # them, and a 0 would be a fabricated figure.
            assert run.final_equity is None
            assert run.total_return_pct is None
            assert run.max_drawdown_pct is None
            assert run.win_rate_pct is None
            assert run.num_trades is None

            assert await _equity_points(session, run.id) == []
            assert await _trades(session, run.id) == []

            # And it is really persisted, not just an in-memory object.
            stored = (
                await session.execute(select(BacktestRun).where(BacktestRun.id == run.id))
            ).scalar_one()
            assert stored.status is BacktestRunStatus.FAILED


@pytest.mark.asyncio
async def test_a_window_with_no_bars_at_all_fails_rather_than_reporting_a_zero_return() -> None:
    """An empty window is a data gap, not a strategy that did nothing. A
    SUCCEEDED run reporting final_equity == starting_cash would read as the
    latter while the fact is the former."""
    warmup_only = [_bar(day, Decimal(100)) for day in WARMUP_DATES]

    async with db_session() as session:
        async with validated_version(session, DEFINITION_ALL_IN) as version:
            run, _ = await _run(
                session, version, bars=warmup_only, signals=BUY_THEN_SELL_SIGNALS
            )
            assert run.status is BacktestRunStatus.FAILED
            assert run.error_detail is not None
            assert "no window to replay" in run.error_detail
            assert run.final_equity is None


@pytest.mark.asyncio
async def test_the_provider_is_asked_once_for_a_buffered_range_covering_warmup() -> None:
    """One provider call, not two: warmup and window come back together and
    are split on the real dates the bars carry.

    The fetch reaches back `warmup_bars * 2 + 10` = 3*2+10 = 16 calendar
    days before start_date (2026-03-02 -> 2026-02-14) to find 3 TRADING
    bars - the same generous, holiday-unaware buffer v1's own trading-day
    helpers document, except this engine then counts what actually arrived.
    """
    provider = FakeBarProvider(_full_bars())
    async with db_session() as session:
        async with validated_version(session, DEFINITION_ALL_IN) as version:
            with patch(
                "apps.api.app.backtesting.engine_v2.generate_signals",
                return_value=BUY_THEN_SELL_SIGNALS,
            ):
                await run_strategy_backtest(
                    session=session,
                    strategy_version=version,
                    symbol=TEST_SYMBOL,
                    bar_interval="1d",
                    start_date=START_DATE,
                    end_date=END_DATE,
                    starting_cash=Decimal(10_000),
                    bar_provider=provider,
                    risk_limits=_risk_limits(),
                    portfolio_limits=None,
                    requested_by_user_id=None,
                )

    assert provider.calls == [(TEST_SYMBOL, "1d", date(2026, 2, 14), END_DATE)]
