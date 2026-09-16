"""Iterative, hold-out-validated strategy improvement (Phase 72,
docs/DECISIONS.md D090).

    start from a validated definition
    repeat up to `max_iterations`:
        generate every one-parameter variant of the CURRENT BEST
        backtest each on the TRAIN window
        take the best-scoring variant
        if it does not beat the incumbent on train      -> stop, converged
        backtest it on the VALIDATE window
        if it also beats the incumbent there            -> accept, persist a
                                                            new version
        otherwise                                       -> reject, and stop

**This module is a machine for overfitting unless it is built not to be.**
Propose-measure-keep-repeat against a single window will always find a
definition that fits that window's noise; run it long enough and it will
report a wonderful number that predicts nothing. Three rules, all
structural rather than advisory, are what stop that here:

1. **Two windows, and the second is not looked at until the first has
   already chosen.** Candidates are generated and ranked on TRAIN only. The
   VALIDATE window is touched exactly once per iteration, for the single
   candidate that already won on train. A variant that never won on train
   never contributes a validation run at all - visible in the data as a
   step row with `validate_backtest_run_id IS NULL`.

2. **Acceptance requires improvement on BOTH.** Beating the incumbent on
   train is what earns a candidate the right to be judged; beating it on
   held-out data is what earns acceptance. Train-only improvement is the
   signature of fitting noise, and it is recorded as a rejection saying so.

3. **The search stops at the first rejection.** It does not go on trying
   the second-best candidate, the third, and so on - that is exactly how a
   hold-out gets consumed: judge enough candidates against it and it stops
   being held out. One validation per iteration is the budget.

**Nothing here is an LLM.** Variants come from
`strategies/perturbation.py`'s deterministic one-factor-at-a-time
generator, and every number comes from `engine_v2` replaying real bars
through the real Risk Engine and the real cost model. The same inputs
produce the same search every time, which is what makes a recorded run
re-checkable.

**The loop does not decide what "better" means beyond return.** It ranks on
`total_return_pct` and says so. That is a deliberately crude objective -
it ignores drawdown, trade count and the shape of the equity curve - and
`_OBJECTIVE_NOTE` carries that caveat into every step's stored reason so a
reader of the record is told, not left to assume.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.backtesting.costs import CostModel
from apps.api.app.backtesting.engine_v2 import run_strategy_backtest
from apps.api.app.core.logging import get_logger
from apps.api.app.db.models import (
    BacktestRun,
    BacktestRunStatus,
    StrategyImprovementRun,
    StrategyImprovementRunStatus,
    StrategyImprovementStep,
    StrategyVersion,
    StrategyVersionStatus,
)
from apps.api.app.marketdata.bar_provider import HistoricalBarProvider
from apps.api.app.portfolio_manager.models import PortfolioLimits
from apps.api.app.risk.models import RiskLimits
from apps.api.app.strategies.perturbation import generate_perturbed_definitions
from apps.api.app.strategies.service import compute_definition_hash

logger = get_logger(__name__)

_ERROR_DETAIL_MAX_LENGTH = 500
_REASON_MAX_LENGTH = 300

DEFAULT_MAGNITUDE_PCT = Decimal("10")
"""How far each parameter is nudged per iteration, as a percentage.

10% is small enough that a variant is recognisably the same strategy - the
search refines a definition rather than wandering into an unrelated one -
and large enough to move an integer indicator period by at least one bar
for any period of 10 or more."""

_OBJECTIVE_NOTE = "ranked on total_return_pct only (ignores drawdown and trade count)"


@dataclass(frozen=True)
class _Scored:
    """One candidate and what the train window said about it."""

    index: int
    parameter_path: str
    direction: str
    original_value: Decimal
    perturbed_value: Decimal
    definition: dict
    run: BacktestRun

    @property
    def return_pct(self) -> Decimal | None:
        if self.run.status is not BacktestRunStatus.SUCCEEDED:
            return None
        return self.run.total_return_pct


async def _backtest(
    session: AsyncSession,
    *,
    version: StrategyVersion,
    symbol: str,
    bar_interval: str,
    start_date: date,
    end_date: date,
    starting_cash: Decimal,
    bar_provider: HistoricalBarProvider,
    risk_limits: RiskLimits,
    portfolio_limits: PortfolioLimits | None,
    cost_model: CostModel,
    quantity_precision: int,
    requested_by_user_id: uuid.UUID | None,
) -> BacktestRun:
    """One ordinary backtest. The REAL engine, unchanged - a search that
    scored candidates with its own private replay could drift out of
    agreement with what the rest of the platform reports about the same
    definition, and the whole value of a recorded search is that its steps
    open as normal runs."""
    return await run_strategy_backtest(
        session=session,
        strategy_version=version,
        symbol=symbol,
        bar_interval=bar_interval,
        start_date=start_date,
        end_date=end_date,
        starting_cash=starting_cash,
        bar_provider=bar_provider,
        risk_limits=risk_limits,
        portfolio_limits=portfolio_limits,
        cost_model=cost_model,
        quantity_precision=quantity_precision,
        requested_by_user_id=requested_by_user_id,
    )


def _json_native(value: object) -> object:
    """A definition with every `Decimal` replaced by a JSON-native number.

    **Why this is here and not in `perturbation.py`.** That module writes
    an EXACT Decimal into a variant on purpose - so `0.25 * 0.9` is
    precisely `0.225` rather than binary float's
    `0.225000000000000005...` - and `test_no_returned_value_is_ever_a_binary_float`
    pins it. That in-memory contract is right and is left alone; the
    problem is only at the DATABASE boundary, which is where this converts.

    `strategy_versions.definition` is JSONB, and a Decimal raises "Object
    of type Decimal is not JSON serializable" on write. It never surfaced
    before Phase 72 because `robustness.py` uses variants in memory only
    and never persists one - this loop is the first thing that does.

    **The float round-trips exactly** for the magnitudes involved, which is
    what makes this safe rather than a quiet precision loss: `float(str(d))`
    then Python's shortest-round-trip `repr` returns the same digits, and
    every reader in this codebase parses the field back through
    `Decimal(str(...))` (see `engine_v2._desired_quantity`). An integral
    Decimal becomes an `int` so an indicator period stays a period rather
    than turning into `20.0`.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(str(value))
    if isinstance(value, dict):
        return {k: _json_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_native(v) for v in value]
    return value


async def _draft_version(
    session: AsyncSession, *, strategy_id: uuid.UUID, definition: dict
) -> StrategyVersion:
    """A VALIDATED version for a candidate definition.

    Marked validated directly rather than routed through the draft ->
    validate flow: a perturbation of an already-validated definition
    changes one numeric leaf within its own legal range, so it is valid by
    construction, and `generate_perturbed_definitions` clamps rather than
    emitting an out-of-range value.

    **Version numbers are assigned from the strategy's current maximum**,
    so a search interleaves with hand-authored versions without colliding.
    """
    from sqlalchemy import func, select

    next_number = (
        await session.execute(
            select(func.coalesce(func.max(StrategyVersion.version_number), 0) + 1).where(
                StrategyVersion.strategy_id == strategy_id
            )
        )
    ).scalar_one()

    # Converted at the write, not at generation - see `_json_native`.
    stored = _json_native(definition)
    assert isinstance(stored, dict)
    version = StrategyVersion(
        id=uuid.uuid4(),
        strategy_id=strategy_id,
        version_number=next_number,
        definition=stored,
        definition_hash=compute_definition_hash(stored),
        status=StrategyVersionStatus.VALIDATED,
        validated_at=datetime.now(UTC),
    )
    session.add(version)
    await session.flush()
    return version


def _record_step(
    session: AsyncSession,
    run: StrategyImprovementRun,
    *,
    iteration: int,
    candidate: _Scored | None,
    validate_run: BacktestRun | None,
    accepted: bool,
    reason: str,
    created_version_id: uuid.UUID | None = None,
) -> None:
    """One row per candidate the search actually formed an opinion about."""
    session.add(
        StrategyImprovementStep(
            id=uuid.uuid4(),
            improvement_run_id=run.id,
            iteration=iteration,
            candidate_index=candidate.index if candidate else -1,
            parameter_path=candidate.parameter_path if candidate else "(none)",
            direction=candidate.direction if candidate else "-",
            original_value=candidate.original_value if candidate else None,
            perturbed_value=candidate.perturbed_value if candidate else None,
            train_backtest_run_id=candidate.run.id if candidate else None,
            validate_backtest_run_id=validate_run.id if validate_run else None,
            train_return_pct=candidate.return_pct if candidate else None,
            validate_return_pct=(
                validate_run.total_return_pct
                if validate_run and validate_run.status is BacktestRunStatus.SUCCEEDED
                else None
            ),
            accepted=accepted,
            accepted_reason=reason[:_REASON_MAX_LENGTH],
            created_version_id=created_version_id,
        )
    )


async def run_improvement_loop(
    *,
    session: AsyncSession,
    strategy_id: uuid.UUID,
    base_version: StrategyVersion,
    symbol: str,
    bar_interval: str,
    train_start_date: date,
    train_end_date: date,
    validate_start_date: date,
    validate_end_date: date,
    starting_cash: Decimal,
    max_iterations: int,
    bar_provider: HistoricalBarProvider,
    risk_limits: RiskLimits,
    portfolio_limits: PortfolioLimits | None,
    cost_model: CostModel,
    quantity_precision: int,
    magnitude_pct: Decimal = DEFAULT_MAGNITUDE_PCT,
    requested_by_user_id: uuid.UUID | None,
) -> StrategyImprovementRun:
    """Runs the search and returns the committed row, always terminal.

    **Returns a FAILED run; never raises one.** Same posture as
    `run_strategy_backtest` (D072) and `run_backfill_job` (D070): the row
    exists before any work starts, so a failure is recorded with its real
    message rather than vanishing into a 500.

    **A SUCCEEDED run may have improved nothing.** `best_version_id` stays
    NULL and `best_*_return_pct` mirror the baseline when no candidate
    survived validation. "The baseline was not beaten out-of-sample" is a
    real and frequently correct answer, and it is reported as success
    because the search did exactly what it was asked to.
    """
    run = StrategyImprovementRun(
        id=uuid.uuid4(),
        strategy_id=strategy_id,
        base_version_id=base_version.id,
        symbol=symbol,
        bar_interval=bar_interval,
        train_start_date=train_start_date,
        train_end_date=train_end_date,
        validate_start_date=validate_start_date,
        validate_end_date=validate_end_date,
        starting_cash=starting_cash,
        max_iterations=max_iterations,
        status=StrategyImprovementRunStatus.RUNNING,
        requested_by_user_id=requested_by_user_id,
    )
    session.add(run)
    await session.flush()

    async def _finish_failed(detail: str) -> StrategyImprovementRun:
        run.status = StrategyImprovementRunStatus.FAILED
        run.error_detail = detail[:_ERROR_DETAIL_MAX_LENGTH]
        run.completed_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(run)
        return run

    try:
        async def backtest(
            version: StrategyVersion, start: date, end: date
        ) -> BacktestRun:
            """The shared arguments, bound once. A closure rather than a
            `**common` dict: a dict of mixed value types erases them, and
            the type checker then cannot see that `cost_model` is a
            CostModel rather than an object - which is exactly the kind of
            mistake it is here to catch on a path that places simulated
            money."""
            return await _backtest(
                session,
                version=version,
                symbol=symbol,
                bar_interval=bar_interval,
                start_date=start,
                end_date=end,
                starting_cash=starting_cash,
                bar_provider=bar_provider,
                risk_limits=risk_limits,
                portfolio_limits=portfolio_limits,
                cost_model=cost_model,
                quantity_precision=quantity_precision,
                requested_by_user_id=requested_by_user_id,
            )

        # The baseline, on BOTH windows. Without the baseline's own
        # validate-window figure there is nothing to judge a candidate's
        # out-of-sample result against, and "better than the baseline"
        # would silently become "positive", which is a different claim.
        base_train = await backtest(base_version, train_start_date, train_end_date)
        if base_train.status is not BacktestRunStatus.SUCCEEDED:
            return await _finish_failed(
                "BASELINE_FAILED on the train window, so there is nothing to improve on: "
                f"{base_train.error_detail}"
            )
        base_validate = await backtest(
            base_version, validate_start_date, validate_end_date
        )
        if base_validate.status is not BacktestRunStatus.SUCCEEDED:
            return await _finish_failed(
                "BASELINE_FAILED on the validate window, so no candidate could be judged "
                f"out-of-sample: {base_validate.error_detail}"
            )

        run.baseline_train_return_pct = base_train.total_return_pct
        run.baseline_validate_return_pct = base_validate.total_return_pct

        incumbent_definition = base_version.definition
        incumbent_version = base_version
        incumbent_train = base_train.total_return_pct or Decimal(0)
        incumbent_validate = base_validate.total_return_pct or Decimal(0)
        candidates_tested = 0
        iterations_run = 0

        for iteration in range(1, max_iterations + 1):
            iterations_run = iteration
            perturbations = generate_perturbed_definitions(
                incumbent_definition, magnitude_pct=magnitude_pct
            )
            if not perturbations:
                _record_step(
                    session, run, iteration=iteration, candidate=None, validate_run=None,
                    accepted=False,
                    reason=(
                        "NO_PERTURBABLE_PARAMETERS: this definition has no numeric parameter "
                        "to vary (all_in sizing with no indicator periods), so there is "
                        "nothing for the search to try."
                    ),
                )
                break

            scored: list[_Scored] = []
            for index, p in enumerate(perturbations):
                version = await _draft_version(
                    session, strategy_id=strategy_id, definition=p.definition
                )
                train_run = await backtest(version, train_start_date, train_end_date)
                candidates_tested += 1
                scored.append(
                    _Scored(
                        index=index, parameter_path=p.parameter_path, direction=p.direction,
                        original_value=p.original_value, perturbed_value=p.perturbed_value,
                        definition=p.definition, run=train_run,
                    )
                )

            usable = [c for c in scored if c.return_pct is not None]
            if not usable:
                _record_step(
                    session, run, iteration=iteration, candidate=None, validate_run=None,
                    accepted=False,
                    reason=(
                        f"ALL_CANDIDATES_FAILED: none of {len(scored)} variant(s) could be "
                        "replayed on the train window - most often a longer indicator period "
                        "outrunning the available warmup history."
                    ),
                )
                break

            best = max(usable, key=lambda c: c.return_pct or Decimal(0))
            best_return = best.return_pct or Decimal(0)

            if best_return <= incumbent_train:
                _record_step(
                    session, run, iteration=iteration, candidate=best, validate_run=None,
                    accepted=False,
                    reason=(
                        f"CONVERGED: the best variant returned {best_return}% on train against "
                        f"the incumbent's {incumbent_train}%, so no nudge of any single "
                        f"parameter improves it. The held-out window was NOT consulted. "
                        f"[{_OBJECTIVE_NOTE}]"
                    ),
                )
                break

            # It won on train. ONE validation run - the hold-out budget.
            best_version = await _draft_version(
                session, strategy_id=strategy_id, definition=best.definition
            )
            validate_run = await backtest(
                best_version, validate_start_date, validate_end_date
            )
            if validate_run.status is not BacktestRunStatus.SUCCEEDED:
                _record_step(
                    session, run, iteration=iteration, candidate=best, validate_run=validate_run,
                    accepted=False,
                    reason=(
                        "VALIDATION_FAILED: the winning variant could not be replayed on the "
                        f"held-out window ({validate_run.error_detail}), so it cannot be shown "
                        "to generalize and is not accepted."
                    ),
                )
                break

            validate_return = validate_run.total_return_pct or Decimal(0)
            if validate_return <= incumbent_validate:
                _record_step(
                    session, run, iteration=iteration, candidate=best, validate_run=validate_run,
                    accepted=False,
                    reason=(
                        f"OVERFIT_REJECTED: better on train ({best_return}% vs "
                        f"{incumbent_train}%) but NOT on held-out data ({validate_return}% vs "
                        f"{incumbent_validate}%). Improving in-sample while failing "
                        "out-of-sample is the signature of fitting noise, so the search stops "
                        "here rather than spending more of the hold-out."
                    ),
                )
                break

            _record_step(
                session, run, iteration=iteration, candidate=best, validate_run=validate_run,
                accepted=True, created_version_id=best_version.id,
                reason=(
                    f"ACCEPTED: {best.parameter_path} {best.original_value} -> "
                    f"{best.perturbed_value} improved train "
                    f"({incumbent_train}% -> {best_return}%) AND held-out "
                    f"({incumbent_validate}% -> {validate_return}%). [{_OBJECTIVE_NOTE}]"
                ),
            )
            incumbent_definition = best.definition
            incumbent_version = best_version
            incumbent_train = best_return
            incumbent_validate = validate_return

        run.iterations_run = iterations_run
        run.candidates_tested = candidates_tested
        run.best_train_return_pct = incumbent_train
        run.best_validate_return_pct = incumbent_validate
        # NULL when nothing was accepted - the baseline still stands, and
        # pointing `best_version_id` at it would claim the search produced
        # something it did not.
        run.best_version_id = (
            incumbent_version.id if incumbent_version.id != base_version.id else None
        )
        run.status = StrategyImprovementRunStatus.SUCCEEDED
        run.completed_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(run)

        logger.info(
            "strategy_improvement_succeeded",
            improvement_run_id=str(run.id),
            strategy_id=str(strategy_id),
            symbol=symbol,
            iterations=iterations_run,
            candidates=candidates_tested,
            improved=run.best_version_id is not None,
        )
        return run

    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        return await _finish_failed(f"{type(exc).__name__}: {exc}")
