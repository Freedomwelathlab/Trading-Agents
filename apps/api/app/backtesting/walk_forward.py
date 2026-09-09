"""Sequential out-of-sample consistency testing of one validated
`StrategyVersion` (Phase 57): the SAME fixed definition replayed over
several non-overlapping historical windows, so that "this strategy returned
X%" can be checked against "and it did so repeatedly" rather than against
one window that may simply have suited it.

**Deliberately NOT "walk-forward optimization".** The textbook technique
fits parameters on an in-sample slice and then measures them on the
following out-of-sample slice. This platform has no parameter-fitting step
anywhere - a `StrategyDefinition`'s rules are fixed by whoever authored them
(D071), and nothing in this codebase searches, tunes, or re-derives them -
so there is no in-sample half to fit on and nothing to re-fit between
windows. What this module measures is CONSISTENCY of one unchanged
definition across periods. Calling it optimization would claim a capability
that does not exist, which docs/TRADING_SAFETY.md's no-fabrication rule
forbids as squarely as inventing a price would.

**Every window starts fresh at the same `starting_cash`.** They do not
compound through a running balance. The question here is "does this behave
similarly in different periods", and compounding would let one window's
result set the capital base for the next, so a good first window would
inflate every later window's absolute figures and a bad one would suppress
them - turning a consistency statistic into a path-dependent one. A single
ordinary backtest over the whole range already answers the compounding
question, and it is one request away.

**`run_strategy_backtest` is CALLED, never re-implemented.** Each window is
an ordinary, fully persisted `BacktestRun` produced by
apps/api/app/backtesting/engine_v2.py exactly as `POST .../backtests`
produces one - same risk engine, same portfolio manager, same broker fill
math, same bar store. A second replay path here could drift out of agreement
with the first while both kept passing their own tests, and a window's
result would then no longer mean the same thing as a backtest's.

**Returns a terminal row; never raises.** Same posture as
`run_strategy_backtest` (D072) and for the same reason: the
`walk_forward_runs` row is created and flushed before any work happens, so a
failure is something to RECORD - what was asked for, when, by whom, and
exactly why it could not be answered - rather than something that vanishes
into a 500 leaving a RUNNING row stranded.
"""

import statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.backtesting.engine_v2 import run_strategy_backtest
from apps.api.app.core.logging import get_logger
from apps.api.app.db.models import (
    BacktestRun,
    BacktestRunStatus,
    StrategyVersion,
    WalkForwardRun,
    WalkForwardRunStatus,
    WalkForwardWindow,
)
from apps.api.app.marketdata.bar_provider import HistoricalBarProvider
from apps.api.app.portfolio_manager.models import PortfolioLimits
from apps.api.app.risk.models import RiskLimits

logger = get_logger(__name__)

_ERROR_DETAIL_MAX_LENGTH = 500
"""`walk_forward_runs.error_detail` is String(500), exactly as
`backtest_runs.error_detail` is. A message is truncated to fit rather than
being allowed to fail the very write that records the failure."""

MIN_WINDOWS = 2
"""Below this, there is no consistency to report. One window is an ordinary
backtest wearing a different name - it has no spread, no best-versus-worst,
and a standard deviation that does not exist - so producing a walk-forward
"result" from it would dress a single sample up as evidence about
repeatability. A run that cannot reach two windows FAILS and says so."""


@dataclass(frozen=True)
class _Window:
    """One planned window, before anything is run.

    `start_date` / `end_date` are both INCLUSIVE - the exact pair handed to
    `run_strategy_backtest`, and the exact pair stored on the
    `walk_forward_windows` row, so what was requested, what ran and what was
    recorded are the same two dates throughout.
    """

    index: int
    start_date: date
    end_date: date

    @property
    def day_count(self) -> int:
        """Calendar days covered, inclusive of both endpoints. CALENDAR
        days, not trading days: this module plans windows on the calendar
        because it has no market calendar (the same limitation engine.py's
        `_trading_days_between` documents), and how many bars actually exist
        inside a window is the bar store's answer, not this function's."""
        return (self.end_date - self.start_date).days + 1


def compute_windows(
    *, overall_start_date: date, overall_end_date: date, window_days: int
) -> list[_Window]:
    """Sequential, non-overlapping windows covering
    `[overall_start_date, overall_end_date]`, `window_index` starting at 0.

    **Boundary convention.** Window `i` opens on
    `overall_start_date + i * window_days` days and is INCLUSIVE of both its
    own endpoints, so a full window spans exactly `window_days` calendar days
    and ends on `start + window_days - 1`. The next window opens on the day
    after this one ends, which makes the series non-overlapping and
    gap-free: window 0 of 7-day windows starting on the 1st is [1st, 7th],
    window 1 is [8th, 14th]. Planning stops once a window's start would fall
    after `overall_end_date`, and no window ever extends past it.

    **A final PARTIAL window** - one clipped short by `overall_end_date` - is
    kept only if it still covers at least `window_days // 2` days, and is
    otherwise dropped silently. A short tail is not wrong, it is just noisy:
    a two-day window's return sits in the same mean and the same standard
    deviation as a thirty-day window's while measuring far less, so a
    half-window-or-shorter tail contributes more distortion to a consistency
    statistic than information. Dropping it costs nothing that a caller
    cannot recover by asking for a range that divides evenly.
    """
    windows: list[_Window] = []
    index = 0
    while True:
        start = overall_start_date + timedelta(days=index * window_days)
        if start > overall_end_date:
            break
        end = min(start + timedelta(days=window_days - 1), overall_end_date)
        window = _Window(index=index, start_date=start, end_date=end)
        # One test covers both cases: a full window's `day_count` is exactly
        # `window_days` and clears `window_days // 2` trivially, so only a
        # tail clipped by `overall_end_date` can ever fail here - and when it
        # does, it was the last window anyway.
        if window.day_count >= window_days // 2:
            windows.append(window)
        index += 1
    return windows


async def _finish_failed(
    session: AsyncSession, run: WalkForwardRun, detail: str
) -> WalkForwardRun:
    """Resolve the already-created row to FAILED and hand it back.

    The aggregate statistic columns are left NULL, never 0 - a run that
    computed no mean has no mean, and `0.00%` would read as "flat", which is
    a fabricated number (docs/TRADING_SAFETY.md). Whatever counts the caller
    already established (`num_windows` and friends) stay as they were set:
    those are observations, not statistics.
    """
    run.status = WalkForwardRunStatus.FAILED
    run.error_detail = detail[:_ERROR_DETAIL_MAX_LENGTH]
    run.completed_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(run)
    logger.info(
        "walk_forward_run_failed",
        walk_forward_run_id=str(run.id),
        strategy_version_id=str(run.strategy_version_id),
        symbol=run.symbol,
    )
    return run


def _apply_aggregates(run: WalkForwardRun, returns: list[Decimal]) -> None:
    """Fill in the four return statistics from the SUCCEEDED windows' own
    `total_return_pct` values. Never called with an empty list.

    `stddev_return_pct` is the SAMPLE standard deviation (`statistics.stdev`,
    dividing by `n - 1`), not the population one: these windows are a sample
    of the periods this strategy could have been run over, not the whole
    population of them, and `pstdev` would systematically understate the
    spread. `statistics.stdev` raises on `n < 2`, so a single succeeded
    window leaves the column NULL - which is the honest answer, since one
    observation genuinely has no spread, and is not the same statement as
    `0.0000` ("it never varied").
    """
    run.mean_return_pct = statistics.mean(returns)
    run.stddev_return_pct = statistics.stdev(returns) if len(returns) > 1 else None
    run.best_window_return_pct = max(returns)
    run.worst_window_return_pct = min(returns)


async def run_walk_forward(
    *,
    session: AsyncSession,
    strategy_version: StrategyVersion,
    symbol: str,
    bar_interval: str,
    overall_start_date: date,
    overall_end_date: date,
    window_days: int,
    starting_cash: Decimal,
    bar_provider: HistoricalBarProvider,
    risk_limits: RiskLimits,
    portfolio_limits: PortfolioLimits | None,
    requested_by_user_id: uuid.UUID | None,
) -> WalkForwardRun:
    """Replays `strategy_version` over each sequential window of
    `[overall_start_date, overall_end_date]` and returns the committed
    `WalkForwardRun` row, always in a terminal status.

    `strategy_version` MUST already be `VALIDATED` - the caller's
    responsibility, identical to `run_strategy_backtest`'s contract, and
    apps/api/app/api/routes/walk_forward.py answers 409 before ever calling
    this. Not re-checked here for the same reason it is not re-checked there:
    one enforcement point, not two that can disagree.

    A window whose own backtest FAILED (most often a bar-store gap specific
    to that sub-period) contributes NOTHING to the averages - not a zero.
    Averaging in 0% for a window that was never measured would report a
    fabricated result for a period this system has no data about, and would
    drag the mean toward zero in exact proportion to how much data is
    missing. It is still counted in `num_windows` and still gets its own
    `walk_forward_windows` row pointing at its real, persisted failed run,
    so the gap is visible rather than silently smoothed over.

    FAILS, with a row rather than an exception, when fewer than
    `MIN_WINDOWS` windows fit the requested range, or when every window's
    backtest failed.
    """
    windows = compute_windows(
        overall_start_date=overall_start_date,
        overall_end_date=overall_end_date,
        window_days=window_days,
    )

    run = WalkForwardRun(
        id=uuid.uuid4(),
        strategy_version_id=strategy_version.id,
        requested_by_user_id=requested_by_user_id,
        symbol=symbol,
        bar_interval=bar_interval,
        overall_start_date=overall_start_date,
        overall_end_date=overall_end_date,
        window_days=window_days,
        starting_cash=starting_cash,
        status=WalkForwardRunStatus.RUNNING,
    )
    session.add(run)
    # Flushed before any work, exactly as `run_strategy_backtest` flushes
    # its own row: the id has to exist for the whole duration of the run,
    # which is what makes every failure below recordable.
    await session.flush()

    if len(windows) < MIN_WINDOWS:
        # `num_windows` stays NULL here rather than being set to
        # `len(windows)`: that column counts windows ATTEMPTED, and this
        # branch attempts none.
        return await _finish_failed(
            session,
            run,
            f"INSUFFICIENT_WINDOWS: {overall_start_date.isoformat()}.."
            f"{overall_end_date.isoformat()} at {window_days} day(s) per window yields "
            f"{len(windows)} window(s), but at least {MIN_WINDOWS} are needed to say "
            "anything about consistency. Widen the range or shorten window_days.",
        )

    try:
        window_runs: list[tuple[_Window, BacktestRun]] = []
        for window in windows:
            # The real engine, unchanged - this returns an already-committed
            # BacktestRun in a terminal status and never raises (D072), so a
            # single bad window cannot abort the sequence.
            backtest_run = await run_strategy_backtest(
                session=session,
                strategy_version=strategy_version,
                symbol=symbol,
                bar_interval=bar_interval,
                start_date=window.start_date,
                end_date=window.end_date,
                starting_cash=starting_cash,
                bar_provider=bar_provider,
                risk_limits=risk_limits,
                portfolio_limits=portfolio_limits,
                requested_by_user_id=requested_by_user_id,
            )
            session.add(
                WalkForwardWindow(
                    id=uuid.uuid4(),
                    walk_forward_run_id=run.id,
                    window_index=window.index,
                    start_date=window.start_date,
                    end_date=window.end_date,
                    backtest_run_id=backtest_run.id,
                )
            )
            window_runs.append((window, backtest_run))
    except Exception as exc:
        # Broad on purpose, matching `run_strategy_backtest`'s own posture.
        # The engine already absorbs every per-window failure into a FAILED
        # BacktestRun, so reaching here means something outside a window's
        # replay went wrong - and the walk-forward row already exists to
        # record it, which beats a 500 that strands it in RUNNING forever.
        return await _finish_failed(session, run, str(exc))

    # `total_return_pct` is NOT NULL in practice on a SUCCEEDED run, but the
    # column is nullable, and a statistic must never be computed over a value
    # that turned out not to be there.
    succeeded_returns = [
        backtest_run.total_return_pct
        for _window, backtest_run in window_runs
        if backtest_run.status is BacktestRunStatus.SUCCEEDED
        and backtest_run.total_return_pct is not None
    ]

    # Counts, unlike the return statistics, are real observations even when
    # nothing succeeded: "3 attempted, 0 succeeded" is a measurement.
    run.num_windows = len(window_runs)
    run.num_succeeded_windows = len(succeeded_returns)

    if not succeeded_returns:
        first_failure = next(
            (
                backtest_run
                for _window, backtest_run in window_runs
                if backtest_run.status is not BacktestRunStatus.SUCCEEDED
            ),
            None,
        )
        reason = (
            first_failure.error_detail
            if first_failure is not None and first_failure.error_detail
            else "no error detail was recorded"
        )
        return await _finish_failed(
            session,
            run,
            f"ALL_WINDOWS_FAILED: all {len(window_runs)} window backtest(s) of {symbol!r} "
            f"failed, so there is nothing to aggregate. The first one "
            f"({windows[0].start_date.isoformat()}..{windows[0].end_date.isoformat()}) "
            f"said: {reason}",
        )

    run.num_profitable_windows = sum(1 for value in succeeded_returns if value > 0)
    _apply_aggregates(run, succeeded_returns)
    run.status = WalkForwardRunStatus.SUCCEEDED
    run.completed_at = datetime.now(UTC)
    await session.commit()
    # Re-read so the returned row carries what the database actually stores:
    # every aggregate column is NUMERIC(10,4), so a mean or a standard
    # deviation with more places than that is rounded on write and the caller
    # must see the stored value rather than the pre-write one.
    await session.refresh(run)

    logger.info(
        "walk_forward_run_succeeded",
        walk_forward_run_id=str(run.id),
        strategy_version_id=str(strategy_version.id),
        symbol=symbol,
        bar_interval=bar_interval,
        num_windows=run.num_windows,
        num_succeeded_windows=run.num_succeeded_windows,
        num_profitable_windows=run.num_profitable_windows,
    )
    return run
