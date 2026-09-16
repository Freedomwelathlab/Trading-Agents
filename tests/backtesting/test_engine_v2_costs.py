"""Costs actually reach the equity curve, the trade ledger and the run row
(Phase 70, D088). DB-backed, against real Postgres.

The fixtures, the risk limits and the `validated_version` lifecycle are
IMPORTED from tests/backtesting/test_engine_v2.py rather than rebuilt, so
these tests replay through exactly the setup the engine's own tests use and
differ from them in one variable only: the cost model. A second, subtly
different harness could make a cost difference look like an engine
difference.

**The central case is a FLAT round trip** - bought and sold at the same
price. Its profit is definitionally zero before costs, so whatever the run
loses is the cost model and nothing else, to the cent. That is a stronger
statement than "costs reduced the return", which a sign error on one leg
would also satisfy.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from apps.api.app.backtesting.costs import CostModel
from apps.api.app.backtesting.strategy import Signal
from apps.api.app.db.models import BacktestRun, BacktestRunStatus, BacktestTrade
from apps.api.app.marketdata.bar_provider import Bar
from tests.backtesting.test_engine_v2 import (
    START_DATE,
    TEST_SYMBOL,
    WARMUP_DATES,
    WINDOW_DATES,
    _run,
    db_session,
    validated_version,
)

FLAT_PRICE = Decimal(100)
"""Every bar closes here. A round trip over flat bars earns exactly zero
before costs, which is what makes the loss below attributable."""

STARTING_CASH = Decimal(10_000)

# fixed_notional rather than all_in ON PURPOSE. `all_in` at this starting
# cash is capped by the Risk Engine's 30%-of-equity position limit, which
# would make the filled quantity - and therefore every figure below - a
# function of the risk limits as well as of the cost model. $1,000 of
# notional sits far enough under that cap that the quantity is decided by
# the sizing rule alone, so the arithmetic in each test is closed-form.
DEFINITION_FIXED_NOTIONAL = {
    "indicators": [{"id": "sma_2", "type": "sma", "period": 2}],
    "entry_rule": {"op": "crosses_above", "left": "close", "right": "sma_2"},
    "exit_rule": {"op": "crosses_below", "left": "close", "right": "sma_2"},
    "position_sizing": {"type": "fixed_notional", "amount": 1000},
}

# 10bps fee + 5bps slippage = 15bps adverse on each side.
COSTED = CostModel(fee_bps=Decimal(10), slippage_bps=Decimal(5))

# Buy on the window's first bar, sell on its second, then nothing.
BUY_THEN_SELL = [Signal.HOLD] * 3 + [Signal.BUY, Signal.SELL] + [Signal.HOLD] * 3

# ---------------------------------------------------------------------
# Derived BY HAND from the constants above, not captured from a run:
#
#   buy price       = 100 * (1 + 15/10_000)          = 100.15
#   quantity        = floor(min(10_000, 1_000) / 100.15)
#                   = floor(9.985...)                = 9
#   cash after buy  = 10_000 - 9 * 100.15            = 9_098.65
#   sell price      = 100 * (1 - 15/10_000)          =  99.85
#   cash after sell = 9_098.65 + 9 * 99.85           = 9_997.30
#
#   mid notional per side = 9 * 100                  = 900
#   fee per side          = 900 * 10/10_000          =   0.90
#   slippage per side     = 900 *  5/10_000          =   0.45
#   two sides             = 1.80 fees + 0.90 slippage=   2.70
#
# and 10_000 - 9_997.30 = 2.70 exactly. The round trip lost precisely its
# own costs and nothing else.
# ---------------------------------------------------------------------
EXPECTED_QUANTITY = Decimal(9)
EXPECTED_FINAL_EQUITY = Decimal("9997.30")
EXPECTED_FEES = Decimal("1.80")
EXPECTED_SLIPPAGE = Decimal("0.90")
EXPECTED_TOTAL_COST = Decimal("2.70")


def _flat_bars() -> list[Bar]:
    return [
        Bar(
            symbol=TEST_SYMBOL,
            bar_interval="1d",
            ts=datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
            close=FLAT_PRICE,
            source="test-engine-v2-costs",
        )
        for day in [*WARMUP_DATES, *WINDOW_DATES]
    ]


async def _costed_run(session, version, cost_model: CostModel) -> BacktestRun:
    # `_run` hands back `(run, patched_generate_signals)`; only the run
    # matters here, since these tests inject their signals rather than
    # asserting anything about how the evaluator was called.
    run, _ = await _run(
        session,
        version,
        bars=_flat_bars(),
        signals=BUY_THEN_SELL,
        starting_cash=STARTING_CASH,
        cost_model=cost_model,
    )
    return run


# ------------------------------------------------------- the central case


@pytest.mark.asyncio
async def test_a_flat_round_trip_loses_exactly_its_modelled_cost() -> None:
    """Bought and sold at the same price, so the P&L IS the cost model."""
    async with (
        db_session() as session,
        validated_version(session, DEFINITION_FIXED_NOTIONAL) as version,
    ):
        run = await _costed_run(session, version, COSTED)

        assert run.status is BacktestRunStatus.SUCCEEDED
        assert run.final_equity == EXPECTED_FINAL_EQUITY
        assert STARTING_CASH - run.final_equity == EXPECTED_TOTAL_COST


@pytest.mark.asyncio
async def test_the_reported_fees_and_slippage_account_for_the_whole_loss() -> None:
    """The reconciliation an operator will actually do: the two reported
    cost figures must add up to the gap between starting cash and final
    equity. If they did not, one of the two was computed from something the
    replay did not do."""
    async with (
        db_session() as session,
        validated_version(session, DEFINITION_FIXED_NOTIONAL) as version,
    ):
        run = await _costed_run(session, version, COSTED)

        assert run.total_fees == EXPECTED_FEES
        assert run.total_slippage == EXPECTED_SLIPPAGE
        assert run.total_fees + run.total_slippage == STARTING_CASH - run.final_equity


@pytest.mark.asyncio
async def test_the_same_run_without_costs_breaks_exactly_even() -> None:
    """The control. Same bars, same signals, same sizing - only the cost
    model differs - and the flat round trip now returns the account to
    precisely where it started. This is what pins the loss above to the cost
    model rather than to anything else in the replay loop."""
    async with (
        db_session() as session,
        validated_version(session, DEFINITION_FIXED_NOTIONAL) as version,
    ):
        run = await _costed_run(session, version, CostModel.frictionless())

        assert run.final_equity == STARTING_CASH
        assert run.total_return_pct == Decimal(0)
        assert run.total_fees == Decimal(0)
        assert run.total_slippage == Decimal(0)


# --------------------------------------------------- what gets recorded


@pytest.mark.asyncio
async def test_a_flat_trade_is_recorded_as_a_loss_once_costed() -> None:
    """The honesty that motivated this phase. Before Phase 70 this round
    trip's entry and exit were both the bar close, so it graded as neither a
    win nor a loss and contributed a 0% return. Costed, it is what it really
    is: a losing trade. A strategy that round-trips flat repeatedly must not
    show a flat equity curve."""
    async with (
        db_session() as session,
        validated_version(session, DEFINITION_FIXED_NOTIONAL) as version,
    ):
        run = await _costed_run(session, version, COSTED)

        assert run.num_trades == 1
        assert run.win_rate_pct == Decimal(0)

        trade = (
            await session.execute(
                select(BacktestTrade).where(BacktestTrade.backtest_run_id == run.id)
            )
        ).scalar_one()
        assert trade.quantity == EXPECTED_QUANTITY
        # Both legs carry the COSTED price, so the ledger's own return_pct
        # is net - it must not disagree in sign with the equity curve.
        assert trade.entry_price == Decimal("100.150000")
        assert trade.exit_price == Decimal("99.850000")
        assert trade.return_pct < 0


@pytest.mark.asyncio
async def test_the_run_records_the_assumptions_it_was_executed_under() -> None:
    """A return figure without its cost assumption is not reproducible - the
    setting behind it can be edited afterwards. Both rates are copied onto
    the row so a stored result stays self-describing."""
    async with (
        db_session() as session,
        validated_version(session, DEFINITION_FIXED_NOTIONAL) as version,
    ):
        run = await _costed_run(session, version, COSTED)

        assert run.fee_bps == Decimal("10.0000")
        assert run.slippage_bps == Decimal("5.0000")


@pytest.mark.asyncio
async def test_costs_are_recorded_even_on_a_run_that_places_no_trade() -> None:
    """A run that never trades pays nothing - and reports zero, not NULL.
    NULL on these columns means "this run predates cost modelling", which is
    a different claim from "this run modelled costs and incurred none"."""
    async with (
        db_session() as session,
        validated_version(session, DEFINITION_FIXED_NOTIONAL) as version,
    ):
        run, _ = await _run(
            session,
            version,
            bars=_flat_bars(),
            signals=[Signal.HOLD] * 8,
            starting_cash=STARTING_CASH,
            cost_model=COSTED,
        )

        assert run.status is BacktestRunStatus.SUCCEEDED
        assert run.num_trades == 0
        assert run.total_fees == Decimal(0)
        assert run.total_slippage == Decimal(0)
        assert run.final_equity == STARTING_CASH
        # The assumptions are still recorded: the run WAS costed, it simply
        # had no fill to charge.
        assert run.fee_bps == Decimal("10.0000")


@pytest.mark.asyncio
async def test_a_failed_run_still_records_its_cost_assumptions() -> None:
    """The rates are written when the row is created, not on success, so a
    FAILED run says what it was attempted under as well as why it could not
    complete. The provider here is given the window but no warmup bars at
    all, so this fails on InsufficientHistoryError."""
    async with (
        db_session() as session,
        validated_version(session, DEFINITION_FIXED_NOTIONAL) as version,
    ):
        window_only = [
            bar for bar in _flat_bars() if bar.ts.astimezone(UTC).date() >= START_DATE
        ]
        run, _ = await _run(
            session,
            version,
            bars=window_only,
            signals=BUY_THEN_SELL,
            starting_cash=STARTING_CASH,
            cost_model=COSTED,
        )

        assert run.status is BacktestRunStatus.FAILED
        assert run.fee_bps == Decimal("10.0000")
        assert run.slippage_bps == Decimal("5.0000")
        # No replay happened, so there is nothing to total - and a 0 here
        # would claim a costed replay that never ran.
        assert run.total_fees is None
        assert run.total_slippage is None


@pytest.mark.asyncio
async def test_sizing_accounts_for_cost_so_an_all_in_entry_is_still_affordable() -> None:
    """The bug this design avoids. Sizing `all_in` against the bar's close
    and then filling above it proposes a notional larger than the account's
    cash, which the paper broker refuses outright with
    InsufficientFundsError - turning a costed backtest into one that
    silently never enters. Sizing against the price the entry really fills
    at is what keeps the trade happening."""
    definition = {**DEFINITION_FIXED_NOTIONAL, "position_sizing": {"type": "all_in"}}
    async with (
        db_session() as session,
        validated_version(session, definition) as version,
    ):
        run = await _costed_run(session, version, COSTED)

        assert run.status is BacktestRunStatus.SUCCEEDED
        assert run.num_trades == 1, "the all-in entry must still have been affordable"

