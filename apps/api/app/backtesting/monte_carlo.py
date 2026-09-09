"""Seeded bootstrap resampling of one completed `BacktestRun`'s trade
returns into a distribution of simulated outcomes (Phase 57).

**What this answers, and what it does not.** A backtest reports the one
sequence of trades that actually occurred. This module asks a different
question of that same evidence: how much would the result have varied by
chance alone had those same trade outcomes arrived in a different order,
drawn WITH REPLACEMENT so some repeat and some never occur? That is a
statement about sequence risk already latent in the observed trades - it
is emphatically NOT a forecast, not a different trading history, and not a
claim that any resampled path could have happened. See `MonteCarloRun`'s
class docstring in db/models.py for why only aggregate percentiles are
persisted.

**A SIBLING of engine_v2.py, and it reuses rather than re-derives.** The
drawdown of each simulated equity path is measured by
`metrics.compute_max_drawdown_pct` - the exact function that produced the
source run's own `max_drawdown_pct` - so the two numbers mean the same
thing and cannot drift apart. Nothing here touches the signal evaluator,
the Risk Engine, the Portfolio Manager or the paper broker: the input is a
list of already-realised percentage returns, and no bar is read, no trade
is re-decided, and no market data is involved at all.

**`run_monte_carlo` returns a terminal row; it does not raise.** Same
posture as `run_strategy_backtest` (D072) and `run_backfill_job` (D070):
the row is created before any work, so every outcome - including "this
backtest has too few trades to resample" - is a real, auditable resource
rather than a 500 that leaves a RUNNING row stranded.

**Synchronous, inline, in the request.** `num_simulations x len(trades)`
Decimal multiplications is the entire cost; at this surface's ceiling
(10,000 simulations over a few dozen trades) that is a fraction of a
second of pure CPU. There is deliberately no queue, no thread pool and no
background worker - matching every other compute-in-request path in this
codebase, and it is exactly what lets the run row be created and finished
inside one transaction with nothing able to race the update.
"""

import random
import statistics
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.backtesting.metrics import compute_max_drawdown_pct
from apps.api.app.core.logging import get_logger
from apps.api.app.db.models import (
    BacktestRun,
    BacktestTrade,
    MonteCarloRun,
    MonteCarloRunStatus,
)

logger = get_logger(__name__)

MIN_TRADES_FOR_RESAMPLING = 5
"""Fewer than five completed round trips is refused rather than resampled.

This is a judgment call, stated as one - there is no threshold the
statistics hand you. The reasoning: a bootstrap sample of size `n` drawn
with replacement from `n` observations is dominated by whichever few
observations exist, and with n=2 or n=3 a meaningful share of simulated
paths are literally "the same single trade repeated". The percentiles such
a run reports would look like a distribution while carrying almost no
information about one, which is a worse outcome than refusing: a confident
number nobody should trust. Five is where the repetition stops being the
dominant feature; it is still a small sample, and a caller reading a
five-trade run's p5/p95 should treat them accordingly.
"""

_ERROR_DETAIL_MAX_LENGTH = 500
"""`monte_carlo_runs.error_detail` is String(500) - the same truncation
engine_v2 applies, so recording a failure can never itself fail the write."""


def _percentile(sorted_values: list[Decimal], pct: float) -> Decimal:
    """The `pct`th percentile of an ALREADY-SORTED list, by linear
    interpolation between the two nearest ranks.

    This is the standard "linear"/R-type-7 definition - the default
    `numpy.percentile` uses - written out here rather than pulled in as a
    dependency: it is six lines of arithmetic, and adding numpy to this
    project to compute two percentiles would be a large dependency bought
    for a small function.

    The interpolation position is `(n - 1) * pct / 100`. An exact integer
    position returns that element unchanged; a fractional one returns the
    weighted blend of its two neighbours. All of it in `Decimal` - the
    inputs are money and percentages, and this codebase does not let a
    binary float touch either, so `pct` is converted through
    `Decimal(str(...))` rather than `Decimal(float)`.

    Raises `ValueError` on an empty list: there is no honest percentile of
    nothing, and returning 0 would fabricate one.
    """
    if not sorted_values:
        raise ValueError("cannot take a percentile of an empty list")
    if len(sorted_values) == 1:
        return sorted_values[0]

    position = Decimal(len(sorted_values) - 1) * Decimal(str(pct)) / Decimal(100)
    lower_index = int(position)
    fraction = position - lower_index
    if fraction == 0:
        return sorted_values[lower_index]
    lower = sorted_values[lower_index]
    upper = sorted_values[lower_index + 1]
    return lower + (upper - lower) * fraction


async def _load_trade_returns(session: AsyncSession, backtest_run_id: uuid.UUID) -> list[Decimal]:
    """The source run's completed round-trip returns, in a DETERMINISTIC
    order (`entry_date`, then `id` as the tiebreaker).

    The order matters far more than it looks like it should: resampling
    draws from this list by index, so the same seed only reproduces the
    same simulation if the list itself comes back the same way twice.
    Without an explicit ORDER BY, Postgres is free to return these rows in
    any order it likes, and a persisted `random_seed` would then be a
    promise this module could not keep.

    Rows with a NULL `return_pct` - the schema's room for a position still
    open at the end of a window - are skipped rather than counted as zero:
    an unclosed position has no realised return, and inventing a 0% one
    would put a fabricated observation into the sample.
    """
    rows = (
        (
            await session.execute(
                select(BacktestTrade)
                .where(BacktestTrade.backtest_run_id == backtest_run_id)
                .order_by(BacktestTrade.entry_date.asc(), BacktestTrade.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [row.return_pct for row in rows if row.return_pct is not None]


def _simulate_path(
    *, returns: list[Decimal], starting_cash: Decimal, rng: random.Random
) -> tuple[Decimal, Decimal]:
    """One simulated path: draw `len(returns)` returns WITH REPLACEMENT,
    apply them in the drawn order, and report `(final_equity,
    max_drawdown_pct)`.

    Compounding, not summing - `equity *= (1 + return_pct / 100)` per drawn
    trade - because that is what a sequence of percentage returns on a
    single account actually does, and it is the reason ORDER matters at all
    (a -50% followed by a +50% does not return you to where you started).
    All `Decimal`; a float would introduce a representation error into a
    money figure, which this codebase permits nowhere.

    The equity curve handed to `compute_max_drawdown_pct` STARTS at
    `starting_cash`, before any trade. That leading point is load-bearing:
    without it a path whose very first drawn trade loses money would report
    that loss as no drawdown at all, since the curve's own first element is
    the function's initial peak.
    """
    drawn = rng.choices(returns, k=len(returns))
    equity = starting_cash
    curve = [equity]
    for return_pct in drawn:
        equity = equity * (Decimal(1) + return_pct / Decimal(100))
        curve.append(equity)
    return equity, compute_max_drawdown_pct(curve)


async def run_monte_carlo(
    *,
    session: AsyncSession,
    backtest_run: BacktestRun,
    num_simulations: int,
    requested_by_user_id: uuid.UUID | None,
    seed: int | None = None,
) -> MonteCarloRun:
    """Resamples `backtest_run`'s trade returns `num_simulations` times and
    returns the committed `MonteCarloRun` row, always in a terminal status.

    `backtest_run` MUST already be `SUCCEEDED` and `num_simulations` must
    already be inside its permitted range - both are the caller's
    responsibility, checked once in
    apps/api/app/api/routes/monte_carlo.py (409 and 422 respectively)
    rather than twice in two places that could disagree. That is the same
    single-enforcement-point rule `run_strategy_backtest` states about a
    version being VALIDATED.

    **`seed` is for tests and for replaying an existing row.** Left at
    `None` - which is what the route always passes - a seed is drawn from
    `random.SystemRandom().getrandbits(63)`: the OS entropy source, so the
    seed itself is unpredictable, and 63 bits so it fits `BigInteger`
    (which is why that column is not this schema's usual `Integer`). The
    simulation downstream of it uses a plain `random.Random(seed)` and
    never `SystemRandom`, because determinism given the seed is the entire
    point of persisting one: `random_seed` is written on the row either way,
    so anyone holding the row can regenerate this exact distribution later.

    **Returns a FAILED row; does not raise.** Too few trades to resample is
    the expected failure and names both the actual count and the minimum;
    the broad `except` below catches everything else for the same reason
    `run_strategy_backtest`'s does.
    """
    resolved_seed = random.SystemRandom().getrandbits(63) if seed is None else seed

    run = MonteCarloRun(
        id=uuid.uuid4(),
        backtest_run_id=backtest_run.id,
        requested_by_user_id=requested_by_user_id,
        num_simulations=num_simulations,
        random_seed=resolved_seed,
        status=MonteCarloRunStatus.RUNNING,
    )
    session.add(run)
    # Flushed before any work, exactly as engine_v2 flushes its own run row:
    # the row (and its id) must exist for the whole duration so that a
    # failure below is something recorded rather than something lost. This
    # is also why the too-few-trades check happens INSIDE the try below
    # rather than before the row is created - a refusal is a result.
    await session.flush()

    try:
        returns = await _load_trade_returns(session, backtest_run.id)
        if len(returns) < MIN_TRADES_FOR_RESAMPLING:
            raise ValueError(
                f"INSUFFICIENT_TRADES: backtest run {backtest_run.id} has {len(returns)} "
                f"completed trade(s) with a recorded return; at least "
                f"{MIN_TRADES_FOR_RESAMPLING} are required to resample meaningfully."
            )

        rng = random.Random(resolved_seed)  # noqa: S311 (simulation, not cryptography)
        final_equities: list[Decimal] = []
        drawdowns: list[Decimal] = []
        for _ in range(num_simulations):
            final_equity, drawdown = _simulate_path(
                returns=returns, starting_cash=backtest_run.starting_cash, rng=rng
            )
            final_equities.append(final_equity)
            drawdowns.append(drawdown)

        ruined = sum(1 for equity in final_equities if equity <= 0)
        sorted_equities = sorted(final_equities)
        sorted_drawdowns = sorted(drawdowns)
    except Exception as exc:
        # Broad on purpose, matching run_strategy_backtest: every failure
        # here is a real outcome of a run this system was asked to perform,
        # and the row already exists to record it. A FAILED row carrying the
        # real message is strictly more useful than a 500 that strands a
        # RUNNING row forever.
        run.status = MonteCarloRunStatus.FAILED
        run.error_detail = str(exc)[:_ERROR_DETAIL_MAX_LENGTH]
        run.completed_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(run)
        logger.info(
            "monte_carlo_run_failed",
            monte_carlo_run_id=str(run.id),
            backtest_run_id=str(backtest_run.id),
            error_type=type(exc).__name__,
        )
        return run

    run.num_trades_resampled = len(returns)
    run.median_final_equity = statistics.median(sorted_equities)
    run.p5_final_equity = _percentile(sorted_equities, 5)
    run.p95_final_equity = _percentile(sorted_equities, 95)
    run.median_max_drawdown_pct = statistics.median(sorted_drawdowns)
    run.p5_max_drawdown_pct = _percentile(sorted_drawdowns, 5)
    run.p95_max_drawdown_pct = _percentile(sorted_drawdowns, 95)
    # A percentage, like every other `_pct` column here - the SHARE of
    # simulated paths that ended at or below zero equity, not a count.
    run.probability_of_ruin_pct = Decimal(ruined) / Decimal(num_simulations) * Decimal(100)
    run.status = MonteCarloRunStatus.SUCCEEDED
    run.completed_at = datetime.now(UTC)
    await session.commit()
    # Re-read so the returned row carries what the database actually stores:
    # these columns are NUMERIC(20,6) / NUMERIC(10,4), and a caller must see
    # the rounded, stored value rather than the fuller pre-write one.
    await session.refresh(run)

    logger.info(
        "monte_carlo_run_succeeded",
        monte_carlo_run_id=str(run.id),
        backtest_run_id=str(backtest_run.id),
        num_simulations=num_simulations,
        num_trades_resampled=len(returns),
    )
    return run
