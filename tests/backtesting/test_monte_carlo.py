"""The Phase 57 Monte Carlo resampler's own mechanics: the percentile
function, seeded reproducibility, the too-few-trades refusal, ruin
accounting, and the compounding math itself.

`_percentile` is tested as a pure function against hand-computed values.
Everything else is DB-backed against a real Postgres instance, for the same
reason tests/backtesting/test_engine_v2.py is: `run_monte_carlo` creates,
finishes and commits a row, and "it reached SUCCEEDED with these
percentiles" has no meaningful unit-level version.

**Every simulation here passes an EXPLICIT seed.** That is the only way to
assert on a number a random process produced, and asserting that the same
seed reproduces the same row bit-for-bit is the whole reason
`monte_carlo_runs.random_seed` is persisted at all - so it is tested
directly rather than assumed.

`db_session` is imported from tests/backtesting/test_engine_v2.py rather
than redefined, exactly as the API test modules share their own fixtures.

The database is shared with every other test module and is not reset
between tests, so nothing here asserts on an absolute row count outside the
rows it seeded itself.
"""

import contextlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from apps.api.app.backtesting.monte_carlo import (
    MIN_TRADES_FOR_RESAMPLING,
    _percentile,
    run_monte_carlo,
)
from apps.api.app.db.models import (
    BacktestRun,
    BacktestRunStatus,
    BacktestTrade,
    MonteCarloRun,
    MonteCarloRunStatus,
    Strategy,
    StrategyVersion,
    StrategyVersionStatus,
)
from apps.api.app.strategies.service import compute_definition_hash
from tests.backtesting.test_engine_v2 import DEFINITION_ALL_IN, db_session

STARTING_CASH = Decimal(10_000)

# A varied, hand-picked return list: three losers among four winners, so a
# resampled path's outcome genuinely depends on which returns it drew.
MIXED_RETURNS = [
    Decimal("6.72"),
    Decimal("-3.10"),
    Decimal("12.00"),
    Decimal("-8.40"),
    Decimal("2.50"),
    Decimal("19.00"),
    Decimal("-1.00"),
]

AGGREGATE_FIELDS = (
    "num_trades_resampled",
    "median_final_equity",
    "p5_final_equity",
    "p95_final_equity",
    "median_max_drawdown_pct",
    "p5_max_drawdown_pct",
    "p95_max_drawdown_pct",
    "probability_of_ruin_pct",
)


def _aggregates(run: MonteCarloRun) -> tuple:
    """Every computed statistic on a run, as one comparable tuple - what
    "the same simulation" means for the reproducibility assertions below."""
    return tuple(getattr(run, field) for field in AGGREGATE_FIELDS)


@contextlib.asynccontextmanager
async def succeeded_backtest_run(session, returns: list[Decimal]):
    """A SUCCEEDED `BacktestRun` whose trades carry exactly `returns`.

    The trades are written directly rather than produced by running the
    engine: this module tests the resampler, and its entire input is a list
    of realised percentage returns. Generating them through a real backtest
    would make every number here depend on the signal evaluator's crossing
    semantics, which tests/backtesting/test_executor.py owns.

    Entry/exit prices are derived FROM each return so the rows are
    internally consistent - a `return_pct` that disagreed with its own two
    prices would be a row the engine could never write.

    No user and no role: `strategies.owner_user_id` is nullable, and
    ownership is the ROUTE's concern (tests/api/test_monte_carlo.py).

    Teardown deletes `monte_carlo_runs` BEFORE the backtest run, because
    `monte_carlo_runs.backtest_run_id` is ON DELETE RESTRICT - the schema
    genuinely refuses to let a resampling's input disappear, and this
    fixture having to work around that is the guarantee doing its job.
    """
    strategy_id = uuid.uuid4()
    version_id = uuid.uuid4()
    run_id = uuid.uuid4()

    session.add(Strategy(id=strategy_id, owner_user_id=None, name=f"monte-carlo-{strategy_id}"))
    session.add(
        StrategyVersion(
            id=version_id,
            strategy_id=strategy_id,
            version_number=1,
            definition=DEFINITION_ALL_IN,
            definition_hash=compute_definition_hash(DEFINITION_ALL_IN),
            status=StrategyVersionStatus.VALIDATED,
            validated_at=datetime.now(UTC),
        )
    )
    # Flushed in FK order explicitly. The unit of work has no `relationship()`
    # between these three tables to order the inserts by, so a single flush
    # can emit the run before the version it points at.
    await session.flush()
    session.add(
        BacktestRun(
            id=run_id,
            strategy_version_id=version_id,
            requested_by_user_id=None,
            symbol="TESTMC.US",
            bar_interval="1d",
            start_date=date(2026, 3, 2),
            end_date=date(2026, 3, 20),
            starting_cash=STARTING_CASH,
            status=BacktestRunStatus.SUCCEEDED,
            final_equity=STARTING_CASH,
            num_trades=len(returns),
            completed_at=datetime.now(UTC),
        )
    )
    await session.flush()
    entry_price = Decimal(100)
    for index, return_pct in enumerate(returns):
        session.add(
            BacktestTrade(
                id=uuid.uuid4(),
                backtest_run_id=run_id,
                side="buy",
                entry_date=date(2026, 3, 2),
                entry_price=entry_price,
                exit_date=date(2026, 3, 3),
                exit_price=entry_price * (Decimal(1) + return_pct / Decimal(100)),
                quantity=Decimal(10 + index),
                return_pct=return_pct,
            )
        )
    await session.commit()

    run = (
        await session.execute(select(BacktestRun).where(BacktestRun.id == run_id))
    ).scalar_one()
    try:
        yield run
    finally:
        await session.rollback()
        await session.execute(
            delete(MonteCarloRun).where(MonteCarloRun.backtest_run_id == run_id)
        )
        await session.execute(delete(BacktestTrade).where(BacktestTrade.backtest_run_id == run_id))
        await session.execute(delete(BacktestRun).where(BacktestRun.id == run_id))
        await session.execute(delete(Strategy).where(Strategy.id == strategy_id))
        await session.commit()


# --------------------------------------------------------------------------
# _percentile
# --------------------------------------------------------------------------


def test_percentile_at_exact_ranks_returns_the_element_itself() -> None:
    """`(n - 1) * pct / 100` landing on a whole number is the no-
    interpolation case: the answer is that element, untouched.

    For [10,20,30,40,50] (n=5): p0 -> position 0, p50 -> position 2,
    p100 -> position 4.
    """
    values = [Decimal(10), Decimal(20), Decimal(30), Decimal(40), Decimal(50)]

    assert _percentile(values, 0) == Decimal(10)
    assert _percentile(values, 50) == Decimal(30)
    assert _percentile(values, 100) == Decimal(50)


def test_percentile_interpolates_linearly_between_the_two_nearest_ranks() -> None:
    """The interpolated case, hand-computed for [10,20,30,40,50] (n=5):

      p5  -> position (5-1)*0.05 = 0.2 -> 10 + (20-10)*0.2 = 12
      p95 -> position (5-1)*0.95 = 3.8 -> 40 + (50-40)*0.8 = 48
      p25 -> position (5-1)*0.25 = 1.0 -> exactly 20 (an exact rank)

    And for an EVEN-length list [10,20,30,40] (n=4), where p50 has no
    single middle element:

      p50 -> position (4-1)*0.5 = 1.5 -> 20 + (30-20)*0.5 = 25
    """
    values = [Decimal(10), Decimal(20), Decimal(30), Decimal(40), Decimal(50)]

    assert _percentile(values, 5) == Decimal(12)
    assert _percentile(values, 95) == Decimal(48)
    assert _percentile(values, 25) == Decimal(20)

    assert _percentile([Decimal(10), Decimal(20), Decimal(30), Decimal(40)], 50) == Decimal(25)


def test_percentile_of_a_single_value_is_that_value_and_of_nothing_is_an_error() -> None:
    """A one-element distribution has one answer at every percentile. An
    EMPTY one has none - and returning 0 would fabricate a figure, so it
    raises instead."""
    assert _percentile([Decimal("7.5")], 5) == Decimal("7.5")
    assert _percentile([Decimal("7.5")], 95) == Decimal("7.5")

    with pytest.raises(ValueError, match="empty"):
        _percentile([], 50)


# --------------------------------------------------------------------------
# Seeded reproducibility - the reason random_seed is persisted at all
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_same_seed_reproduces_the_same_run_bit_for_bit() -> None:
    """Two independent runs over the same trades with the same explicit
    seed produce identical statistics, and a DIFFERENT seed does not.

    This is the property the whole persisted-seed design exists to
    guarantee - that `random_seed` alone is enough to regenerate a
    distribution the schema deliberately does not store - so it is asserted
    directly rather than taken on faith. It is also what pins the
    resampling to `random.Random(seed)`: a `SystemRandom` in the loop would
    fail this test, which is the point.
    """
    async with db_session() as session:
        async with succeeded_backtest_run(session, MIXED_RETURNS) as backtest_run:
            first = await run_monte_carlo(
                session=session,
                backtest_run=backtest_run,
                num_simulations=50,
                requested_by_user_id=None,
                seed=12_345,
            )
            first_aggregates = _aggregates(first)

            second = await run_monte_carlo(
                session=session,
                backtest_run=backtest_run,
                num_simulations=50,
                requested_by_user_id=None,
                seed=12_345,
            )
            other = await run_monte_carlo(
                session=session,
                backtest_run=backtest_run,
                num_simulations=50,
                requested_by_user_id=None,
                seed=987_654_321,
            )

            assert first.status is MonteCarloRunStatus.SUCCEEDED
            assert first.id != second.id
            assert first.random_seed == second.random_seed == 12_345
            assert _aggregates(second) == first_aggregates
            assert _aggregates(other) != first_aggregates
            assert other.random_seed == 987_654_321


@pytest.mark.asyncio
async def test_a_successful_run_persists_ordered_percentiles_and_a_seed() -> None:
    """The shape of a SUCCEEDED row: every statistic computed, no error, and
    p5 <= median <= p95 for both distributions - an ordering that holds by
    construction and would expose a percentile mixed up with its
    neighbour."""
    async with db_session() as session:
        async with succeeded_backtest_run(session, MIXED_RETURNS) as backtest_run:
            run = await run_monte_carlo(
                session=session,
                backtest_run=backtest_run,
                num_simulations=200,
                requested_by_user_id=None,
                seed=4_242,
            )

            assert run.status is MonteCarloRunStatus.SUCCEEDED
            assert run.error_detail is None
            assert run.completed_at is not None
            assert run.num_simulations == 200
            assert run.num_trades_resampled == len(MIXED_RETURNS)
            assert run.random_seed == 4_242

            assert run.p5_final_equity <= run.median_final_equity <= run.p95_final_equity
            assert (
                run.p5_max_drawdown_pct
                <= run.median_max_drawdown_pct
                <= run.p95_max_drawdown_pct
            )
            # A varied return list must not collapse to a single outcome -
            # if it did, the resampling would not be resampling.
            assert run.p5_final_equity < run.p95_final_equity


@pytest.mark.asyncio
async def test_a_seed_is_generated_and_persisted_when_none_is_supplied() -> None:
    """The route never passes a seed. One is drawn from `SystemRandom` and
    written to the row regardless, because a run nobody can reproduce is
    exactly what this column exists to prevent. 63 bits, so it fits
    `BigInteger`."""
    async with db_session() as session:
        async with succeeded_backtest_run(session, MIXED_RETURNS) as backtest_run:
            run = await run_monte_carlo(
                session=session,
                backtest_run=backtest_run,
                num_simulations=100,
                requested_by_user_id=None,
            )
            assert run.status is MonteCarloRunStatus.SUCCEEDED
            assert 0 <= run.random_seed < 2**63


# --------------------------------------------------------------------------
# Refusals and ruin
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_too_few_trades_is_a_persisted_failed_run_naming_both_counts() -> None:
    """Four trades is below the five-trade floor, so nothing is simulated -
    and the refusal is a real, kept row rather than an exception, matching
    `run_strategy_backtest`'s posture. `error_detail` names the actual count
    AND the minimum, so the caller knows how far short they are."""
    async with db_session() as session:
        four_returns = [Decimal(5), Decimal(-2), Decimal(3), Decimal(-1)]
        async with succeeded_backtest_run(session, four_returns) as backtest_run:
            run = await run_monte_carlo(
                session=session,
                backtest_run=backtest_run,
                num_simulations=100,
                requested_by_user_id=None,
                seed=1,
            )

            assert run.status is MonteCarloRunStatus.FAILED
            assert run.completed_at is not None
            assert "4" in run.error_detail
            assert str(MIN_TRADES_FOR_RESAMPLING) in run.error_detail
            # Never a fabricated statistic for a run that simulated nothing.
            assert run.num_trades_resampled is None
            assert run.median_final_equity is None
            assert run.p5_final_equity is None
            assert run.p95_final_equity is None
            assert run.median_max_drawdown_pct is None
            assert run.probability_of_ruin_pct is None


@pytest.mark.asyncio
async def test_all_winning_trades_produce_exactly_zero_probability_of_ruin() -> None:
    """No drawn sequence of gains can end at or below zero equity, so the
    figure is exactly 0 - a computed zero, not a null, because this run DID
    simulate. Drawdown is 0 for the same structural reason: a monotonically
    rising curve never dips below its own running peak."""
    async with db_session() as session:
        winners = [Decimal(5), Decimal(7), Decimal(3), Decimal(11), Decimal(2)]
        async with succeeded_backtest_run(session, winners) as backtest_run:
            run = await run_monte_carlo(
                session=session,
                backtest_run=backtest_run,
                num_simulations=200,
                requested_by_user_id=None,
                seed=77,
            )

            assert run.status is MonteCarloRunStatus.SUCCEEDED
            assert run.probability_of_ruin_pct == Decimal(0)
            assert run.median_max_drawdown_pct == Decimal(0)
            assert run.median_final_equity > STARTING_CASH


@pytest.mark.asyncio
async def test_a_total_loss_among_the_trades_produces_a_real_probability_of_ruin() -> None:
    """One -100% trade in the sample (a position taken to zero) is enough
    that some resampled paths draw it and end at exactly zero equity, while
    paths that never draw it survive - so the reported probability is
    strictly between 0 and 100 rather than degenerate at either end.

    The four -50% trades on their own compound to 10,000 * 0.5^4 = 625,
    which is emphatically not ruin; only the -100% draw produces it. That is
    what makes this a test of the ruin CONDITION (final equity <= 0) rather
    than of "the strategy lost money".
    """
    async with db_session() as session:
        with_wipeout = [
            Decimal(-100),
            Decimal(-50),
            Decimal(-50),
            Decimal(-50),
            Decimal(-50),
        ]
        async with succeeded_backtest_run(session, with_wipeout) as backtest_run:
            run = await run_monte_carlo(
                session=session,
                backtest_run=backtest_run,
                num_simulations=500,
                requested_by_user_id=None,
                seed=2_026,
            )

            assert run.status is MonteCarloRunStatus.SUCCEEDED
            assert Decimal(0) < run.probability_of_ruin_pct < Decimal(100)


# --------------------------------------------------------------------------
# The compounding math itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identical_returns_make_every_path_identical_and_pin_the_compounding() -> None:
    """When every trade has the SAME return, resampling cannot vary the
    outcome - every drawn sequence is the same multiset in a different order
    and compounding is commutative - so all three equity percentiles collapse
    onto one exactly derivable number.

    Five trades of +10% on 10,000: 10,000 * 1.1^5 = 16,105.10, to the cent.

    That makes this a direct test of the compounding arithmetic alone,
    independent of the sampling: if the engine summed returns instead of
    compounding them it would report 15,000, and if it applied `return_pct`
    without dividing by 100 the number would be absurd.

    Max drawdown is 0 on every path for the same reason as the all-winners
    case, and ruin is 0.
    """
    async with db_session() as session:
        identical = [Decimal("10.0000")] * 5
        async with succeeded_backtest_run(session, identical) as backtest_run:
            run = await run_monte_carlo(
                session=session,
                backtest_run=backtest_run,
                num_simulations=50,
                requested_by_user_id=None,
                seed=9,
            )

            expected = Decimal("16105.10")
            assert run.median_final_equity == expected
            assert run.p5_final_equity == expected
            assert run.p95_final_equity == expected
            assert run.median_max_drawdown_pct == Decimal(0)
            assert run.p95_max_drawdown_pct == Decimal(0)
            assert run.probability_of_ruin_pct == Decimal(0)


@pytest.mark.asyncio
async def test_the_drawdown_curve_starts_at_starting_cash() -> None:
    """Five identical -10% trades, so again every path is the same one:
    10,000 -> 9,000 -> 8,100 -> 7,290 -> 6,561 -> 5,904.90.

    Measured from a curve that STARTS at 10,000, the peak-to-trough decline
    is (10,000 - 5,904.90) / 10,000 = 40.951%. Measured from a curve that
    omitted the pre-trade point - the bug this test exists to catch - the
    first loss would be invisible and the answer would be 34.39%, because
    9,000 would be the curve's own opening peak.
    """
    async with db_session() as session:
        losers = [Decimal("-10.0000")] * 5
        async with succeeded_backtest_run(session, losers) as backtest_run:
            run = await run_monte_carlo(
                session=session,
                backtest_run=backtest_run,
                num_simulations=50,
                requested_by_user_id=None,
                seed=11,
            )

            assert run.median_final_equity == Decimal("5904.90")
            assert run.median_max_drawdown_pct == Decimal("40.9510")
            assert run.p5_max_drawdown_pct == Decimal("40.9510")
            # Down 41% is a bad run, but it is not ruin - equity never
            # reached zero, and this column says exactly that.
            assert run.probability_of_ruin_pct == Decimal(0)
