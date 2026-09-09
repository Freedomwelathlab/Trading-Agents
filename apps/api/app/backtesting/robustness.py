"""Parameter-sensitivity testing of one validated `StrategyVersion`
(Phase 58): the same window replayed once with the definition exactly as its
author wrote it, then once more per numeric parameter nudged up and down by
`magnitude_pct`, so that "this strategy returned X%" can be read next to "and
it still roughly does when its own numbers move a little" rather than being
taken on its own.

**Sensitivity analysis, deliberately NOT optimization and NOT a search.**
Nothing here looks for a BETTER parameter set, ranks the perturbed variants
against the baseline as candidates, or writes any variant back to a
`StrategyVersion`. A `StrategyDefinition`'s rules are fixed by whoever
authored them (D071); every perturbed definition this module runs exists
only in memory, for the length of the one replay that measures it. The
question being answered is about the ORIGINAL definition - does its result
survive small changes to its own numbers, or was it balanced on one exact
parameter set that happened to suit this history? Presenting this as
optimization would claim a capability this platform does not have, which
docs/TRADING_SAFETY.md's no-fabrication rule forbids as squarely as
inventing a price would.

**The baseline is RE-RUN here, never read off a previous `BacktestRun`.**
Every number this module reports comes out of the same two engine_v2 helpers
over the same window, the same starting cash and the same limits, so
"perturbed vs baseline" is a comparison between two replays that differ in
exactly one parameter. Trusting a prior backtest row instead would silently
compare against whatever window, cash and limits THAT request happened to
use - and against an engine that may have changed since.

**A perturbation is NOT persisted as a `BacktestRun`**, which is the one
structural difference from Phase 57's walk-forward. A window there replays
the version's own definition, so an ordinary persisted run is exactly the
right record of it. A perturbation replays a definition NO version contains,
and a `backtest_runs` row is a record OF a version's definition - writing one
anyway would file a backtest of rules nobody authored under a version whose
definition says something else. So this module reaches for engine_v2's pure,
already-tested, no-persistence helpers instead, and its own
`robustness_perturbations` rows are where a variant's result lives.

**Returns a terminal row; never raises.** Same posture as
`run_strategy_backtest` (D072) and `run_walk_forward`, for the same reason:
the `robustness_runs` row is created and flushed before any work happens, so
a failure is something to RECORD - what was asked for, when, by whom, and
exactly why it could not be answered - rather than something that vanishes
into a 500 leaving a RUNNING row stranded.
"""

import statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

# `_load_warmup_and_window`, `_replay_window` and `_warmup_bar_count` are
# imported ACROSS MODULES despite their leading underscores, deliberately and
# for the same reason engine_v2.py itself states when it imports engine.py's
# `_attempt_trade`: reusing the exact, already-tested sequence is preferred
# over duplicating it, because a second copy could drift out of agreement
# with the first while both kept passing their own tests. All three are pure
# in the way that matters here - none of them writes to the database - so a
# perturbed variant is replayed by precisely the code an ordinary backtest
# runs, minus only the persistence this module does not want.
from apps.api.app.backtesting.engine_v2 import (
    _load_warmup_and_window,
    _replay_window,
    _warmup_bar_count,
)
from apps.api.app.backtesting.metrics import (
    compute_max_drawdown_pct,
    compute_total_return_pct,
)
from apps.api.app.core.logging import get_logger
from apps.api.app.db.models import (
    RobustnessPerturbationResult,
    RobustnessRun,
    RobustnessRunStatus,
    StrategyVersion,
)
from apps.api.app.marketdata.bar_provider import HistoricalBarProvider
from apps.api.app.portfolio_manager.models import PortfolioLimits
from apps.api.app.risk.models import RiskLimits
from apps.api.app.strategies.perturbation import (
    Perturbation,
    generate_perturbed_definitions,
)

logger = get_logger(__name__)

_ERROR_DETAIL_MAX_LENGTH = 500
"""Both `robustness_runs.error_detail` and
`robustness_perturbations.error_detail` are String(500), exactly as
`backtest_runs.error_detail` is. A message is truncated to fit rather than
being allowed to fail the very write that records the failure."""

PERTURBATION_SUCCEEDED = "succeeded"
PERTURBATION_FAILED = "failed"
"""`robustness_perturbations.status` is a plain string column (the
`BacktestTrade.side` precedent - display-only, never branched on outside its
own row), so its two values are named constants here rather than being
retyped as literals at each of the three places they are written."""


@dataclass(frozen=True)
class _ReplayMetrics:
    """What one completed replay contributes - the two numbers this phase
    compares. Deliberately narrower than `_Replay`: a robustness run has no
    use for equity curves or trade lists, and nothing it does not persist is
    carried around.
    """

    total_return_pct: Decimal
    max_drawdown_pct: Decimal


async def _replay_definition(
    *,
    definition: dict,
    bar_provider: HistoricalBarProvider,
    symbol: str,
    bar_interval: str,
    start_date: date,
    end_date: date,
    starting_cash: Decimal,
    risk_limits: RiskLimits,
    portfolio_limits: PortfolioLimits | None,
) -> _ReplayMetrics:
    """One definition over one window, through the real engine helpers.
    Raises whatever they raise (most often `InsufficientHistoryError`); the
    callers below decide whether that fails a single perturbation or the
    whole run.

    **The warmup is fetched PER DEFINITION, not once and shared.**
    `_warmup_bar_count` is a function of the definition's indicator periods,
    and a perturbed variant is by construction a definition with a different
    period - a +10% nudge on SMA(20) needs 23 warmup bars where the original
    needs 21. Reusing the baseline's shorter warmup for it would hand
    `_replay_window` too few leading bars, and the failure mode is the bad
    one: the evaluator would produce signals from an indicator that is not
    yet defined over its full period rather than raising, quietly reporting a
    number for a variant that was never really replayed. The cost is one
    extra bar-store query per perturbation, against the persisted store
    (D070) and never a vendor - cheap, and paid in exchange for every row
    meaning what it says. It also means a perturbation whose larger period
    outruns the available history fails LOUDLY, with a real
    `InsufficientHistoryError` naming the real numbers, which is exactly the
    outcome `robustness_perturbations.error_detail` exists to record.
    """
    warmup, window = await _load_warmup_and_window(
        bar_provider=bar_provider,
        symbol=symbol,
        bar_interval=bar_interval,
        start_date=start_date,
        end_date=end_date,
        warmup_bars=_warmup_bar_count(definition),
    )
    replay = _replay_window(
        symbol=symbol,
        definition=definition,
        warmup=warmup,
        window=window,
        starting_cash=starting_cash,
        risk_limits=risk_limits,
        portfolio_limits=portfolio_limits,
    )
    return _ReplayMetrics(
        total_return_pct=compute_total_return_pct(
            starting_cash=starting_cash, final_equity=replay.final_equity
        ),
        max_drawdown_pct=compute_max_drawdown_pct(
            [equity for _day, equity in replay.equity_curve]
        ),
    )


async def _finish_failed(
    session: AsyncSession, run: RobustnessRun, detail: str
) -> RobustnessRun:
    """Resolve the already-created row to FAILED and hand it back.

    Every statistic column is left NULL, never 0 - a run that computed no
    mean has no mean, and `0.00%` would read as "it did not move", which is a
    fabricated number (docs/TRADING_SAFETY.md). Counts the caller already
    established stay as they were set: those are observations of what was
    attempted, not statistics about what came back.
    """
    run.status = RobustnessRunStatus.FAILED
    run.error_detail = detail[:_ERROR_DETAIL_MAX_LENGTH]
    run.completed_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(run)
    logger.info(
        "robustness_run_failed",
        robustness_run_id=str(run.id),
        strategy_version_id=str(run.strategy_version_id),
        symbol=run.symbol,
    )
    return run


def _apply_aggregates(
    run: RobustnessRun, *, baseline_return_pct: Decimal, perturbed_returns: list[Decimal]
) -> None:
    """Fill in the three perturbation statistics from the SUCCEEDED
    perturbations' own returns. Never called with an empty list.

    `stddev_perturbed_return_pct` is the SAMPLE standard deviation
    (`statistics.stdev`, dividing by `n - 1`), the same choice and the same
    `n < 2` guard `walk_forward.py::_apply_aggregates` makes: these variants
    are a sample of the perturbations that could have been made, not the
    population of them, and one observation genuinely has no spread - which
    is not the same statement as `0.0000` ("it never varied").

    `max_return_deviation_pct` is the largest ABSOLUTE gap from the baseline,
    so a variant that did 20 points worse and one that did 20 points better
    are equally strong evidence of sensitivity - the question is how much the
    result MOVED, not whether it moved favourably. It is deliberately one
    plain number rather than a composite "robustness score": this phase does
    not define a weighted figure across return, drawdown and whatever else,
    because any weighting would encode a risk preference nobody has stated. A
    future phase can build a score on top of these raw columns; it could not
    recover them from a score.
    """
    run.mean_perturbed_return_pct = statistics.mean(perturbed_returns)
    run.stddev_perturbed_return_pct = (
        statistics.stdev(perturbed_returns) if len(perturbed_returns) > 1 else None
    )
    run.max_return_deviation_pct = max(
        abs(value - baseline_return_pct) for value in perturbed_returns
    )


async def run_robustness_test(
    *,
    session: AsyncSession,
    strategy_version: StrategyVersion,
    symbol: str,
    bar_interval: str,
    start_date: date,
    end_date: date,
    starting_cash: Decimal,
    magnitude_pct: Decimal,
    bar_provider: HistoricalBarProvider,
    risk_limits: RiskLimits,
    portfolio_limits: PortfolioLimits | None,
    requested_by_user_id: uuid.UUID | None,
) -> RobustnessRun:
    """Replays `strategy_version` over `[start_date, end_date]` unchanged and
    then once per +/-`magnitude_pct` perturbation of each of its numeric
    parameters, and returns the committed `RobustnessRun` row, always in a
    terminal status.

    `strategy_version` MUST already be `VALIDATED` - the caller's
    responsibility, identical to `run_strategy_backtest`'s and
    `run_walk_forward`'s contract, and apps/api/app/api/routes/robustness.py
    answers 409 before ever calling this. Not re-checked here for the same
    reason it is not re-checked there: one enforcement point, not two that
    can disagree.

    A perturbation whose own replay FAILED (most often a larger perturbed
    indicator period needing more warmup than the bar store holds) contributes
    NOTHING to the aggregates - not a zero. Averaging in 0% for a variant that
    was never measured would report a fabricated result and drag the mean
    toward zero in exact proportion to how much failed. It is still counted in
    `num_perturbations` and still gets its own row with its own real
    `error_detail`, so the gap is visible rather than silently smoothed over,
    and the remaining perturbations still run: one bad variant does not cost
    the run every other variant's answer.

    Two failure modes end the whole run rather than one row of it. A
    definition with nothing numeric to perturb (`all_in` sizing, no
    indicators) FAILS as `NO_PERTURBABLE_PARAMETERS` - a `SUCCEEDED` run with
    zero perturbations would dress "this question does not apply here" up as
    "this strategy is insensitive", which is a claim about a measurement that
    never happened. And a BASELINE that cannot itself be replayed FAILS as
    `BASELINE_FAILED`, because every number this run exists to report is a
    comparison against it.

    Zero SUCCEEDED perturbations out of several attempted is NOT one of those
    two: it leaves the run SUCCEEDED, with the baseline's own numbers filled
    in, the count of what was attempted, and NULL aggregates. "The strategy
    works exactly as authored and every nudge of it fell over" is a real,
    informative finding about parameter sensitivity - arguably the strongest
    one this module can produce - not an error.
    """
    run = RobustnessRun(
        id=uuid.uuid4(),
        strategy_version_id=strategy_version.id,
        requested_by_user_id=requested_by_user_id,
        symbol=symbol,
        bar_interval=bar_interval,
        start_date=start_date,
        end_date=end_date,
        starting_cash=starting_cash,
        magnitude_pct=magnitude_pct,
        status=RobustnessRunStatus.RUNNING,
    )
    session.add(run)
    # Flushed before any work, exactly as `run_strategy_backtest` and
    # `run_walk_forward` flush their own rows: the id has to exist for the
    # whole duration of the run, which is what makes every failure below
    # recordable rather than lost.
    await session.flush()

    definition = strategy_version.definition
    try:
        baseline = await _replay_definition(
            definition=definition,
            bar_provider=bar_provider,
            symbol=symbol,
            bar_interval=bar_interval,
            start_date=start_date,
            end_date=end_date,
            starting_cash=starting_cash,
            risk_limits=risk_limits,
            portfolio_limits=portfolio_limits,
        )
    except Exception as exc:
        # The baseline is what every perturbation is measured AGAINST, so
        # without it there is nothing to compare and no partial result worth
        # reporting. Recorded as a FAILED row carrying the real underlying
        # message - most often an InsufficientHistoryError naming the actual
        # bar counts - rather than raised.
        return await _finish_failed(
            session,
            run,
            f"BASELINE_FAILED: the unperturbed definition could not be replayed over "
            f"{start_date.isoformat()}..{end_date.isoformat()}, so there is nothing to "
            f"compare perturbations against. {exc}",
        )

    # Recorded before the perturbations run: the baseline is a real,
    # completed measurement on its own, and it stays on the row even if every
    # perturbation below fails.
    run.baseline_return_pct = baseline.total_return_pct
    run.baseline_max_drawdown_pct = baseline.max_drawdown_pct

    try:
        perturbations: list[Perturbation] = generate_perturbed_definitions(
            definition, magnitude_pct=magnitude_pct
        )
    except Exception as exc:
        # Broad on purpose, matching the posture of every sibling
        # orchestrator: the generator is pure and validated input should not
        # break it, but the row already exists to record it if it does, which
        # beats a 500 that strands the row in RUNNING forever.
        return await _finish_failed(
            session, run, f"PERTURBATION_GENERATION_FAILED: {exc}"
        )

    if not perturbations:
        return await _finish_failed(
            session,
            run,
            "NO_PERTURBABLE_PARAMETERS: this definition has no numeric parameters to "
            "perturb (no indicator periods, and position sizing that carries no number), "
            "so its sensitivity to parameter changes is not a question that can be asked "
            "of it. Nothing was measured.",
        )

    results: list[RobustnessPerturbationResult] = []
    perturbed_returns: list[Decimal] = []
    for perturbation in perturbations:
        row = RobustnessPerturbationResult(
            id=uuid.uuid4(),
            robustness_run_id=run.id,
            parameter_path=perturbation.parameter_path,
            direction=perturbation.direction,
            original_value=perturbation.original_value,
            perturbed_value=perturbation.perturbed_value,
            clamped=perturbation.clamped,
            status=PERTURBATION_FAILED,
        )
        try:
            metrics = await _replay_definition(
                definition=perturbation.definition,
                bar_provider=bar_provider,
                symbol=symbol,
                bar_interval=bar_interval,
                start_date=start_date,
                end_date=end_date,
                starting_cash=starting_cash,
                risk_limits=risk_limits,
                portfolio_limits=portfolio_limits,
            )
        except Exception as exc:
            # Caught PER PERTURBATION. One variant that cannot be replayed -
            # a longer period outrunning the available warmup is the ordinary
            # case - is a fact about that variant, not about the run. It is
            # recorded with its own real message, its metric columns stay
            # NULL rather than 0, and the loop continues.
            row.error_detail = str(exc)[:_ERROR_DETAIL_MAX_LENGTH]
        else:
            row.status = PERTURBATION_SUCCEEDED
            row.total_return_pct = metrics.total_return_pct
            row.max_drawdown_pct = metrics.max_drawdown_pct
            perturbed_returns.append(metrics.total_return_pct)
        results.append(row)

    # One bulk add of every row, succeeded and failed alike - a failed
    # perturbation is as much a part of the record as a succeeded one.
    session.add_all(results)

    # Counts, unlike the statistics, are real observations even when nothing
    # succeeded: "6 attempted, 0 succeeded" is a measurement.
    run.num_perturbations = len(results)
    run.num_succeeded_perturbations = len(perturbed_returns)

    if perturbed_returns:
        _apply_aggregates(
            run,
            baseline_return_pct=baseline.total_return_pct,
            perturbed_returns=perturbed_returns,
        )
    # else: every aggregate column stays NULL. The run is still SUCCEEDED -
    # see this function's docstring - because the baseline plus "and every
    # perturbation of it failed" is itself a finding, and NULL is what says
    # the aggregates were never computed.

    run.status = RobustnessRunStatus.SUCCEEDED
    run.completed_at = datetime.now(UTC)
    await session.commit()
    # Re-read so the returned row carries what the database actually stores:
    # every metric column is NUMERIC(10,4), so a mean or a deviation with more
    # places than that is rounded on write and the caller must see the stored
    # value rather than the pre-write one.
    await session.refresh(run)

    logger.info(
        "robustness_run_succeeded",
        robustness_run_id=str(run.id),
        strategy_version_id=str(strategy_version.id),
        symbol=symbol,
        bar_interval=bar_interval,
        num_perturbations=run.num_perturbations,
        num_succeeded_perturbations=run.num_succeeded_perturbations,
    )
    return run
