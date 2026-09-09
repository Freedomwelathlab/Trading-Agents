"""DTOs for the Phase 58 robustness (parameter-sensitivity) routes.

Every money/percentage field is `Decimal`, never `float` - the project-wide
rule for anything financial, stated in schemas_strategy_backtests.py and
backtesting/models.py before it.

The summary/detail split is the one `BacktestRunSummary` vs
`BacktestRunDetailResponse` established and `WalkForwardRunSummary` followed,
for the same reason: a listing of fifty robustness runs should not carry
fifty perturbation breakdowns to render a table of five numbers per row.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from apps.api.app.db.models import RobustnessRunStatus


class CreateRobustnessRunRequest(BaseModel):
    """The market, the window, the capital and how hard to nudge. The
    strategy version is named by the path and supplies every RULE and every
    parameter that gets perturbed; this body carries nothing about the
    strategy itself.

    `bar_interval` is a `Literal["1d"]` for the reason
    `CreateBacktestRunRequest` gives: '1d' is the only interval anything
    ingests or evaluates today, so a 422 here says the actual thing where a
    free string would produce a run that silently finds no bars.
    """

    symbol: str = Field(min_length=1, max_length=32)
    bar_interval: Literal["1d"] = "1d"
    start_date: date
    end_date: date
    starting_cash: Decimal = Field(gt=0)
    magnitude_pct: Decimal = Field(default=Decimal("10"), gt=0, le=50)
    """How far each parameter is nudged, as a percentage of its own original
    value - 10 means every variant moves one parameter to 110% or 90% of what
    it was.

    BOUNDED at 50, and the ceiling is the point rather than a formality. A
    sensitivity test asks whether a result survives a NUDGE; a "perturbation"
    of 200% turns SMA(20) into SMA(60), which is not a slightly different
    strategy but a different one, and reporting how it did under the heading
    "how sensitive is your strategy" would be misleading about what was
    measured. 50% is generous enough to cover any honest robustness question
    and still recognisably a perturbation of the original. The lower bound is
    open (`gt=0`): a 0% nudge would replay the baseline several more times
    under another name.
    """

    @model_validator(mode="after")
    def _validate_range(self) -> "CreateRobustnessRunRequest":
        """Same rule and wording as `CreateBacktestRunRequest`: a range that
        does not move forward is a malformed request, not an empty result."""
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self


class RobustnessPerturbationResultResponse(BaseModel):
    """One perturbed variant: which parameter moved, from what to what, and
    what replaying the result produced.

    `status` is this row's own `"succeeded"` / `"failed"`, independent of the
    parent run's - a SUCCEEDED run can contain failed perturbations, and does
    whenever a larger perturbed period outruns the available warmup history.
    `total_return_pct` / `max_drawdown_pct` are null on exactly those rows,
    where `error_detail` says why; null there always means "not computed",
    never 0.

    `clamped` says the full nudge could not be applied because the parameter
    hit its own legal bound (a period cannot go below 1). Such a variant is a
    real measurement and is reported like any other - the flag is what tells
    a reader its effective magnitude was smaller than the run's.
    """

    id: uuid.UUID
    parameter_path: str
    direction: str
    original_value: Decimal
    perturbed_value: Decimal
    clamped: bool
    status: str
    total_return_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    error_detail: str | None


class RobustnessRunSummary(BaseModel):
    """One robustness run's identity, inputs, baseline and aggregate
    statistics - no perturbation list.

    Every result field is optional because a FAILED run computed none of
    them, and because `stddev_perturbed_return_pct` has no value even on a
    SUCCEEDED run where exactly one perturbation succeeded: one observation
    has no spread, which is not the same statement as `0.0000`. A null here
    always means "not computed", never 0 (docs/TRADING_SAFETY.md's
    no-fabrication rule); `error_detail` is what says why, and is null on a
    successful run.

    `num_perturbations` counts every perturbation ATTEMPTED, including any
    whose own replay failed; the statistics are computed only over the
    succeeded subset, so `num_succeeded_perturbations` is what says how many
    numbers went into them. A SUCCEEDED run with
    `num_succeeded_perturbations == 0` is a real outcome - the baseline ran,
    every nudge of it fell over - and carries null aggregates.

    `max_return_deviation_pct` is the largest absolute gap between a
    succeeded perturbation's return and `baseline_return_pct`. It is
    deliberately one plain number and NOT a composite "robustness score":
    this phase defines no weighted figure across return, drawdown and
    whatever else, because any weighting would encode a risk preference
    nobody stated.
    """

    id: uuid.UUID
    strategy_version_id: uuid.UUID
    symbol: str
    bar_interval: str
    start_date: date
    end_date: date
    starting_cash: Decimal
    magnitude_pct: Decimal
    status: RobustnessRunStatus
    baseline_return_pct: Decimal | None
    baseline_max_drawdown_pct: Decimal | None
    num_perturbations: int | None
    num_succeeded_perturbations: int | None
    mean_perturbed_return_pct: Decimal | None
    stddev_perturbed_return_pct: Decimal | None
    max_return_deviation_pct: Decimal | None
    error_detail: str | None
    created_at: datetime
    completed_at: datetime | None


class RobustnessRunDetailResponse(RobustnessRunSummary):
    """A summary plus the per-perturbation breakdown.

    Subclasses `RobustnessRunSummary` rather than restating its eighteen
    fields - the same precedent `BacktestRunDetailResponse` and
    `WalkForwardRunDetailResponse` set, so the two can never drift into
    disagreeing about what a run's aggregates are.
    """

    perturbations: list[RobustnessPerturbationResultResponse]


class ListRobustnessRunsResponse(BaseModel):
    """Paginated listing envelope - `{items, limit, offset}`, the same shape
    and the same generic `items` key as every other listing in this
    codebase."""

    items: list[RobustnessRunSummary]
    limit: int
    offset: int
