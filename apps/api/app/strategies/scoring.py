"""Strategy scoring - a transparent READ-MODEL over results four earlier
phases already persisted (Phase 59).

**Nothing here is stored.** There is no `strategy_scores` table, no
migration, and no ORM model in this phase, deliberately. Every number a
score reports is recomputed on request from rows that already exist -
`backtest_runs` (Phase 55), `walk_forward_runs` (Phase 57),
`monte_carlo_runs` (Phase 57) and `robustness_runs` (Phase 58). A persisted
score would be a second, staler copy of numbers the source tables already
hold: it would go wrong the moment a newer run lands, and it would let a
reader see a verdict whose inputs no longer exist. Recomputation is cheap
(four indexed lookups per version) and is always exactly as current as the
runs it summarises.

**Never a black box.** Every component carries its own `detail` string
naming the actual column value it came from and the scale that turned that
value into points, and the status carries a `status_reason` naming the
actual counts that produced it. A reader should be able to reconstruct the
arithmetic from the response alone, without this file. That is the whole
reason `ScoreComponent.max_points` is a real field rather than a constant
imported from here: the response has to be self-describing.

**Never optimize solely for ROI, and never invent a number.** `return` is
one of four components and is capped at a quarter of the score - a strategy
cannot rank well on return alone. The other three are the ways this platform
already knows how to say "and it might not be real": out-of-sample
consistency, and parameter stability. D075 and D071 both refused to define a
composite score at their own layer because the raw columns had to come
first; this phase builds one ON TOP of those raw columns and keeps every one
of them visible in the response, which is the order those decisions
described.

**Absent is never zero.** A component whose underlying run does not exist is
LEFT OUT of `components` entirely rather than scored 0 - "nobody has run a
walk-forward test on this" and "this strategy failed its walk-forward test"
are opposite findings, and scoring the first as a zero would state the
second. `max_possible_points` therefore counts only the components that are
actually present (25 each), and `percentage` - never `total_points` - is the
ranking key, so a strategy is not rewarded for having skipped the tests that
could have gone badly. `components_measured` is surfaced next to it so a
reader can always see how much was actually measured, exactly as
docs/TRADING_SAFETY.md's no-fabrication rule requires everywhere else in
this codebase.

**The status heuristic is a deliberate, adjustable first pass, and is
documented as one.** The three thresholds below (a profitable-window ratio
of 0.5, a return deviation of 20 percentage points, and the four component
scales) are simple, stated in one place, and meant to be adjusted as this
platform learns more - they are not a claim of statistical precision and
must not be presented as one. `validated`
here means "both available checks were run and neither raised a flag", not
"this strategy will make money".
"""

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import (
    BacktestRun,
    BacktestRunStatus,
    MonteCarloRun,
    MonteCarloRunStatus,
    RobustnessRun,
    RobustnessRunStatus,
    StrategyVersion,
    WalkForwardRun,
    WalkForwardRunStatus,
    WalkForwardWindow,
)

MAX_COMPONENT_POINTS = Decimal("25")
"""Every component is worth the same 25 points. Equal weighting is itself a
stated choice rather than an absent one: weighting return above stability
(or the reverse) would encode a risk preference nobody here has stated, the
same refusal D075 recorded for `max_return_deviation_pct`."""

RETURN_FULL_POINTS_AT_PCT = Decimal("20")
"""A 20% total return earns the full 25 points; 0% or negative earns 0."""

RISK_ZERO_POINTS_AT_DRAWDOWN_PCT = Decimal("30")
"""A 0% max drawdown earns the full 25 points; 30% or worse earns 0."""

STABILITY_ZERO_POINTS_AT_DEVIATION_PCT = Decimal("20")
"""A perturbed variant that returned exactly what the baseline did earns the
full 25 points; 20+ percentage points of deviation earns 0."""

OVERFIT_PROFITABLE_WINDOW_RATIO = Decimal("0.5")
"""Fewer than half the walk-forward windows profitable is the consistency
warning sign."""

COMPONENT_RETURN = "return"
COMPONENT_RISK = "risk"
COMPONENT_CONSISTENCY = "consistency"
COMPONENT_PARAMETER_STABILITY = "parameter_stability"

STATUS_INSUFFICIENT_DATA = "insufficient_data"
STATUS_OVERFIT_RISK = "overfit_risk"
STATUS_PROMISING = "promising"
STATUS_VALIDATED = "validated"

STATUS_TIERS: dict[str, int] = {
    STATUS_INSUFFICIENT_DATA: 0,
    STATUS_PROMISING: 1,
    STATUS_VALIDATED: 2,
}
"""The ordered tiers a `min_status` filter compares against.

`overfit_risk` is deliberately ABSENT from this mapping rather than given a
rank: it is its own tier, and it satisfies no filter above
`insufficient_data`. A flagged strategy might score numerically well on the
components that were measured, and letting it answer a request for
`min_status=promising` on that basis would surface exactly the strategy the
flag exists to warn about. See `satisfies_min_status`."""

_POINTS_QUANTUM = Decimal("0.0001")
"""Points and percentages are quantized to four decimal places - the same
scale `Numeric(10, 4)` gives every percentage column these scores are
derived from, so a score never reports more precision than its inputs
carry."""


@dataclass(frozen=True)
class ScoreComponent:
    """One measured dimension of a strategy, and the plain-language record of
    how its points were produced.

    `max_points` is a real field, not a constant a reader has to go and look
    up in this module: the response has to be self-describing, and a future
    phase weighting components differently must not silently invalidate
    every already-rendered score. `detail` names the actual column value and
    the actual scale - it is the difference between a number and an
    explanation."""

    name: str
    points: Decimal
    max_points: Decimal
    detail: str


@dataclass(frozen=True)
class StrategyScore:
    """One strategy version's score, and the provenance of every part of it.

    The four `latest_*_run_id` fields are what make this auditable: a reader
    who doubts a component can fetch the exact run it was computed from.
    `latest_backtest_run_id` is non-optional because a version with no
    succeeded backtest has no score at all (`compute_strategy_score` returns
    `None`); the other three are `None` exactly when that kind of run has
    never succeeded for this version.

    `latest_monte_carlo_run_id` is reported but scores NOTHING this phase.
    A Monte Carlo run answers "how much would this have varied by chance
    alone", which is a statement about sequence risk rather than a pass/fail
    signal, and inventing a points scale for it would be exactly the
    unstated risk preference this module refuses to encode elsewhere. It is
    surfaced so a reader can go and look at it.
    """

    strategy_version_id: uuid.UUID
    components: list[ScoreComponent]
    total_points: Decimal
    max_possible_points: Decimal
    percentage: Decimal
    components_measured: int
    status: str
    status_reason: str
    latest_backtest_run_id: uuid.UUID
    latest_walk_forward_run_id: uuid.UUID | None
    latest_monte_carlo_run_id: uuid.UUID | None
    latest_robustness_run_id: uuid.UUID | None


def satisfies_min_status(status: str, min_status: str | None) -> bool:
    """Whether `status` is at or above the `min_status` tier.

    `None` means no filter and admits everything, including `overfit_risk`.
    An `overfit_risk` strategy satisfies only `insufficient_data` (the
    floor) - see `STATUS_TIERS` for why it is a tier of its own rather than
    a rank.
    """
    if min_status is None:
        return True
    threshold = STATUS_TIERS[min_status]
    if status == STATUS_OVERFIT_RISK:
        return threshold == STATUS_TIERS[STATUS_INSUFFICIENT_DATA]
    return STATUS_TIERS[status] >= threshold


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_POINTS_QUANTUM, rounding=ROUND_HALF_UP)


def _clamp_points(value: Decimal) -> Decimal:
    """Points are always within `[0, MAX_COMPONENT_POINTS]`. The clamp is
    what makes the scales honest at the edges: a -40% return and a -5%
    return both earn 0 rather than negative points that would drag down
    components measuring something else, and a 300% return earns the same 25
    as a 20% one rather than dominating every other dimension."""
    return _quantize(min(max(value, Decimal(0)), MAX_COMPONENT_POINTS))


def _return_component(total_return_pct: Decimal) -> ScoreComponent:
    points = total_return_pct / RETURN_FULL_POINTS_AT_PCT * MAX_COMPONENT_POINTS
    return ScoreComponent(
        name=COMPONENT_RETURN,
        points=_clamp_points(points),
        max_points=MAX_COMPONENT_POINTS,
        detail=(
            f"total_return_pct={total_return_pct}% "
            f"(scale: 0%->0pts, {RETURN_FULL_POINTS_AT_PCT}%+->{MAX_COMPONENT_POINTS}pts)"
        ),
    )


def _risk_component(max_drawdown_pct: Decimal) -> ScoreComponent:
    points = (
        (RISK_ZERO_POINTS_AT_DRAWDOWN_PCT - max_drawdown_pct)
        / RISK_ZERO_POINTS_AT_DRAWDOWN_PCT
        * MAX_COMPONENT_POINTS
    )
    return ScoreComponent(
        name=COMPONENT_RISK,
        points=_clamp_points(points),
        max_points=MAX_COMPONENT_POINTS,
        detail=(
            f"max_drawdown_pct={max_drawdown_pct}% "
            f"(scale: 0%->{MAX_COMPONENT_POINTS}pts, "
            f"{RISK_ZERO_POINTS_AT_DRAWDOWN_PCT}%+->0pts)"
        ),
    )


def _consistency_component(profitable: int, windows: int) -> ScoreComponent:
    ratio = Decimal(profitable) / Decimal(windows)
    return ScoreComponent(
        name=COMPONENT_CONSISTENCY,
        points=_clamp_points(ratio * MAX_COMPONENT_POINTS),
        max_points=MAX_COMPONENT_POINTS,
        detail=(
            f"{profitable} of {windows} walk-forward windows profitable "
            f"({_ratio_pct_text(profitable, windows)}%) "
            f"(scale: 0%->0pts, 100%->{MAX_COMPONENT_POINTS}pts)"
        ),
    )


def _parameter_stability_component(max_return_deviation_pct: Decimal) -> ScoreComponent:
    points = (
        (STABILITY_ZERO_POINTS_AT_DEVIATION_PCT - max_return_deviation_pct)
        / STABILITY_ZERO_POINTS_AT_DEVIATION_PCT
        * MAX_COMPONENT_POINTS
    )
    return ScoreComponent(
        name=COMPONENT_PARAMETER_STABILITY,
        points=_clamp_points(points),
        max_points=MAX_COMPONENT_POINTS,
        detail=(
            f"max_return_deviation_pct={max_return_deviation_pct} percentage points "
            f"from baseline (scale: 0->{MAX_COMPONENT_POINTS}pts, "
            f"{STABILITY_ZERO_POINTS_AT_DEVIATION_PCT}+->0pts)"
        ),
    )


def _ratio_pct_text(profitable: int, windows: int) -> str:
    """Whole-percent text for a status/detail string only - never a scored
    number. The points come from the exact `Decimal` ratio."""
    pct = Decimal(profitable) / Decimal(windows) * Decimal(100)
    return str(pct.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _consistency_inputs(run: WalkForwardRun | None) -> tuple[int, int] | None:
    """`(profitable, windows)` when a succeeded walk-forward run really
    carries both counts and ran at least one window, otherwise `None`.

    A `SUCCEEDED` run can still have `NULL` counts, and `num_windows` can be
    0 - the schema makes every aggregate nullable precisely because a run
    that computed nothing must not report a fabricated figure. Dividing by
    that would either crash or invent a ratio, so it is treated as an ABSENT
    component instead: nothing was measured, which is not the same as
    measuring zero.
    """
    if run is None:
        return None
    if run.num_windows is None or run.num_profitable_windows is None:
        return None
    if run.num_windows <= 0:
        return None
    return run.num_profitable_windows, run.num_windows


def build_strategy_score(
    strategy_version_id: uuid.UUID,
    backtest_run: BacktestRun | None,
    walk_forward_run: WalkForwardRun | None = None,
    monte_carlo_run: MonteCarloRun | None = None,
    robustness_run: RobustnessRun | None = None,
) -> StrategyScore | None:
    """The whole scoring model, as a pure function of four already-selected
    rows - no session, no I/O, no clock.

    Split out from `compute_strategy_score` so the model itself is testable
    without a database, and so the "which run is the latest succeeded one"
    question lives in exactly one place (the query below) rather than being
    re-answered inside the arithmetic.

    Returns `None` when there is no backtest to measure - see
    `compute_strategy_score`.
    """
    if backtest_run is None:
        return None

    components: list[ScoreComponent] = []

    # A SUCCEEDED backtest writes both metrics together, so in practice both
    # of these are present whenever the run is. They are still checked,
    # because the columns are nullable and a null must become an absent
    # component rather than a fabricated 0 (the same rule the walk-forward
    # and robustness branches below follow).
    if backtest_run.total_return_pct is not None:
        components.append(_return_component(backtest_run.total_return_pct))
    if backtest_run.max_drawdown_pct is not None:
        components.append(_risk_component(backtest_run.max_drawdown_pct))

    consistency_inputs = _consistency_inputs(walk_forward_run)
    if consistency_inputs is not None:
        components.append(_consistency_component(*consistency_inputs))

    # A SUCCEEDED robustness run whose every perturbation failed carries a
    # NULL deviation (see RobustnessRun's docstring) - real, and not a
    # measurement of stability, so it is absent rather than 0.
    deviation = robustness_run.max_return_deviation_pct if robustness_run else None
    if deviation is not None:
        components.append(_parameter_stability_component(deviation))

    if not components:
        # Structurally reachable only if a SUCCEEDED backtest somehow carries
        # neither metric. Nothing was measured, so there is nothing to score
        # - the same answer as "no backtest at all", rather than a 0/0.
        return None

    total_points = _quantize(sum((item.points for item in components), Decimal(0)))
    max_possible_points = _quantize(
        sum((item.max_points for item in components), Decimal(0))
    )
    percentage = _quantize(total_points / max_possible_points * Decimal(100))

    status, status_reason = _classify(consistency_inputs, deviation)

    return StrategyScore(
        strategy_version_id=strategy_version_id,
        components=components,
        total_points=total_points,
        max_possible_points=max_possible_points,
        percentage=percentage,
        components_measured=len(components),
        status=status,
        status_reason=status_reason,
        latest_backtest_run_id=backtest_run.id,
        latest_walk_forward_run_id=walk_forward_run.id if walk_forward_run else None,
        latest_monte_carlo_run_id=monte_carlo_run.id if monte_carlo_run else None,
        latest_robustness_run_id=robustness_run.id if robustness_run else None,
    )


def _classify(
    consistency_inputs: tuple[int, int] | None, deviation: Decimal | None
) -> tuple[str, str]:
    """The status heuristic, and a reason naming the numbers behind it.

    Return and risk never enter this: they describe how a strategy DID, and
    every strategy with a backtest has them. What this classifies is how much
    checking has actually been done and whether any of it raised a flag - so
    only the consistency and parameter-stability checks are consulted.

    Deliberately simple and deliberately adjustable (see the module
    docstring). ONE warning sign is enough for `overfit_risk`, which
    therefore takes priority over `validated`: a strategy that holds up under
    parameter nudges but falls apart out of sample is not validated, it is
    flagged, and the reason string says which half failed.
    """
    checks_run: list[str] = []
    warnings: list[str] = []

    if consistency_inputs is not None:
        profitable, windows = consistency_inputs
        text = (
            f"walk-forward showed {profitable}/{windows} profitable windows "
            f"({_ratio_pct_text(profitable, windows)}%)"
        )
        if Decimal(profitable) / Decimal(windows) < OVERFIT_PROFITABLE_WINDOW_RATIO:
            warnings.append(
                f"walk-forward showed only {profitable}/{windows} profitable windows "
                f"({_ratio_pct_text(profitable, windows)}%, under the "
                f"{OVERFIT_PROFITABLE_WINDOW_RATIO * 100:.0f}% threshold)"
            )
        else:
            checks_run.append(text)

    if deviation is not None:
        text = f"robustness showed a {deviation} percentage-point maximum return deviation"
        if deviation > STABILITY_ZERO_POINTS_AT_DEVIATION_PCT:
            warnings.append(
                f"robustness showed a {deviation} percentage-point maximum return "
                f"deviation from baseline (over the "
                f"{STABILITY_ZERO_POINTS_AT_DEVIATION_PCT} threshold)"
            )
        else:
            checks_run.append(text)

    available = len(checks_run) + len(warnings)

    if available == 0:
        return (
            STATUS_INSUFFICIENT_DATA,
            "0 of 2 possible checks available: only return and risk were measured. "
            "No walk-forward run and no robustness run have succeeded for this "
            "version, so consistency and parameter stability are unmeasured - not "
            "failed.",
        )

    passed = len(checks_run)
    summary = f"{passed} of {available} available check{'s' if available != 1 else ''} passed"

    if warnings:
        return STATUS_OVERFIT_RISK, f"{summary}: " + "; ".join(warnings + checks_run) + "."

    if available == 2:
        return STATUS_VALIDATED, f"{summary}: " + "; ".join(checks_run) + "."

    missing = (
        "no robustness run has succeeded for this version"
        if consistency_inputs is not None
        else "no walk-forward run has succeeded for this version"
    )
    return STATUS_PROMISING, f"{summary}: " + "; ".join(checks_run) + f"; {missing}."


async def _latest_succeeded_backtest(
    session: AsyncSession, strategy_version_id: uuid.UUID
) -> BacktestRun | None:
    """The newest succeeded backtest this version was DIRECTLY asked for.

    A walk-forward run persists one real `backtest_runs` row per window
    (D074 - the orchestrator calls the ordinary engine rather than a second
    execution path), and those rows carry the same `strategy_version_id`. So
    "the newest backtest_runs row" is not the same thing as "this strategy's
    headline result": the last row written for a version that has ever been
    walk-forward tested is almost always its final WINDOW, covering a third
    or a fifth of the range, produced as an internal step of a different
    analysis.

    Scoring `return` and `risk` off that would describe a fragment the user
    never requested, under a window they would not recognise, and would make
    a version's score change depending on which sub-window happened to be
    written last - while `latest_backtest_run_id` pointed at a run that only
    means anything inside its parent. Window rows are therefore excluded
    here, and the walk-forward run they belong to is what scores the
    consistency component instead - which is the question those windows were
    actually run to answer.

    `walk_forward_windows.backtest_run_id` is NOT NULL, so the `NOT IN` has
    no null-semantics trap.
    """
    return (
        await session.execute(
            select(BacktestRun)
            .where(
                BacktestRun.strategy_version_id == strategy_version_id,
                BacktestRun.status == BacktestRunStatus.SUCCEEDED,
                BacktestRun.id.not_in(select(WalkForwardWindow.backtest_run_id)),
            )
            .order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _latest_succeeded_walk_forward(
    session: AsyncSession, strategy_version_id: uuid.UUID
) -> WalkForwardRun | None:
    return (
        await session.execute(
            select(WalkForwardRun)
            .where(
                WalkForwardRun.strategy_version_id == strategy_version_id,
                WalkForwardRun.status == WalkForwardRunStatus.SUCCEEDED,
            )
            .order_by(WalkForwardRun.created_at.desc(), WalkForwardRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _latest_succeeded_robustness(
    session: AsyncSession, strategy_version_id: uuid.UUID
) -> RobustnessRun | None:
    return (
        await session.execute(
            select(RobustnessRun)
            .where(
                RobustnessRun.strategy_version_id == strategy_version_id,
                RobustnessRun.status == RobustnessRunStatus.SUCCEEDED,
            )
            .order_by(RobustnessRun.created_at.desc(), RobustnessRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _latest_succeeded_monte_carlo(
    session: AsyncSession, strategy_version_id: uuid.UUID
) -> MonteCarloRun | None:
    """Monte Carlo is the one run type with no `strategy_version_id` of its
    own - it hangs off a `BacktestRun`, which is where its provenance already
    lives (see `MonteCarloRun`'s docstring). Reaching a version's Monte Carlo
    runs therefore means joining through the backtests of that version, which
    is exactly what this does rather than denormalizing a column onto a table
    this phase is not allowed - and has no reason - to touch."""
    return (
        await session.execute(
            select(MonteCarloRun)
            .join(BacktestRun, BacktestRun.id == MonteCarloRun.backtest_run_id)
            .where(
                BacktestRun.strategy_version_id == strategy_version_id,
                MonteCarloRun.status == MonteCarloRunStatus.SUCCEEDED,
            )
            .order_by(MonteCarloRun.created_at.desc(), MonteCarloRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def compute_strategy_score(
    session: AsyncSession, strategy_version: StrategyVersion
) -> StrategyScore | None:
    """This version's score, computed fresh from its most recent SUCCEEDED
    run of each type.

    `None` when no succeeded `BacktestRun` exists for the version. That is
    "there is nothing here to measure yet", NOT a score of zero - a strategy
    nobody has backtested has no result to summarise, and giving it a 0 would
    rank it below strategies that did badly, which is a claim about it that
    the data does not support. Callers exclude such strategies from a
    leaderboard entirely.

    The backtest considered is the newest one this version was DIRECTLY
    asked for - a walk-forward run's own per-window `backtest_runs` rows are
    excluded, for the reason `_latest_succeeded_backtest` gives.

    Only SUCCEEDED runs are considered. A FAILED run is a real, kept record
    of an attempt (`error_detail` says why it failed) but it computed no
    metrics, so there is nothing in it to score; a RUNNING one has not
    finished. "Most recent" is by `created_at` with the id as a deterministic
    tiebreaker, matching every other newest-first listing in this codebase.

    Four independent single-row lookups rather than one join: each run type
    has its own `(strategy_version_id, created_at)` index and its own
    "latest" question, and a single query answering all four would need three
    lateral joins to say what four indexed limits already say plainly.
    """
    backtest_run = await _latest_succeeded_backtest(session, strategy_version.id)
    if backtest_run is None:
        return None

    return build_strategy_score(
        strategy_version_id=strategy_version.id,
        backtest_run=backtest_run,
        walk_forward_run=await _latest_succeeded_walk_forward(session, strategy_version.id),
        monte_carlo_run=await _latest_succeeded_monte_carlo(session, strategy_version.id),
        robustness_run=await _latest_succeeded_robustness(session, strategy_version.id),
    )
