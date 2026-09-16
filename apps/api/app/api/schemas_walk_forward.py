"""DTOs for the Phase 57 walk-forward routes.

Every money/percentage field is `Decimal`, never `float` - the project-wide
rule for anything financial, stated in schemas_strategy_backtests.py and
backtesting/models.py before it.

The summary/detail split is the same one `BacktestRunSummary` vs
`BacktestRunDetailResponse` established, for the same reason: a listing of
fifty walk-forward runs should not carry fifty window breakdowns to render a
table of five numbers per row. The detail shape is far lighter here than a
backtest's, though - a window carries its `backtest_run_id` and nothing
else, so a client that wants one window's full equity curve and trade list
fetches `GET /backtest-runs/{id}` for exactly the window it cares about
instead of receiving every window's curve inline.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from apps.api.app.db.models import WalkForwardRunStatus
from apps.api.app.marketdata.bar_provider import BarInterval


class CreateWalkForwardRunRequest(BaseModel):
    """The market, the overall range and how to slice it. The strategy
    version is named by the path and supplies every RULE; this body carries
    nothing about the strategy itself.

    `bar_interval` is the shared `BarInterval` vocabulary for the reason
    `CreateBacktestRunRequest` gives: one closed spelling of each
    interval, so the plain-string column underneath cannot end up
    holding two spellings that never match each other on read.

    `window_days` is in CALENDAR days, matching how
    backtesting/walk_forward.py plans boundaries - it has no market calendar,
    and how many bars actually fall inside a window is the bar store's
    answer, not the request's.
    """

    symbol: str = Field(min_length=1, max_length=32)
    bar_interval: BarInterval = "1d"
    overall_start_date: date
    overall_end_date: date
    window_days: int = Field(gt=0)
    starting_cash: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def _validate_range(self) -> "CreateWalkForwardRunRequest":
        """Same rule and wording as `CreateBacktestRunRequest`: a range that
        does not move forward is a malformed request, not an empty result.

        Deliberately NOT also validating that the range is wide enough for
        two windows. That depends on the boundary and partial-tail rules
        `compute_windows` owns, and re-deriving them here would create a
        second, drift-prone copy of them; too narrow a range is answered by a
        real FAILED run naming the actual window count instead.
        """
        if self.overall_end_date <= self.overall_start_date:
            raise ValueError("overall_end_date must be after overall_start_date")
        return self


class WalkForwardWindowResponse(BaseModel):
    """One window's slice of the range and the ordinary `BacktestRun` that
    was really executed over it.

    `start_date` and `end_date` are both INCLUSIVE - the exact pair handed to
    the backtest engine. `backtest_run_id` is a live handle: the window's
    full equity curve and trade list are one `GET /backtest-runs/{id}` away,
    which is why none of it is duplicated here.
    """

    id: uuid.UUID
    window_index: int
    start_date: date
    end_date: date
    backtest_run_id: uuid.UUID


class WalkForwardRunSummary(BaseModel):
    """One walk-forward run's identity, inputs and aggregate statistics - no
    window list.

    Every aggregate is optional because a FAILED run computed none of them,
    and because `stddev_return_pct` has no value even on a SUCCEEDED run
    where exactly one window succeeded: one observation has no spread, which
    is not the same statement as `0.0000`. A null here always means "not
    computed", never 0 (docs/TRADING_SAFETY.md's no-fabrication rule);
    `error_detail` is what says why, and is null on a successful run.

    `num_windows` counts every window ATTEMPTED, including any whose own
    backtest failed; the return statistics are computed only over the
    succeeded subset, so `num_succeeded_windows` is what says how many
    numbers went into them.
    """

    id: uuid.UUID
    strategy_version_id: uuid.UUID
    symbol: str
    bar_interval: str
    overall_start_date: date
    overall_end_date: date
    window_days: int
    starting_cash: Decimal
    status: WalkForwardRunStatus
    num_windows: int | None
    num_succeeded_windows: int | None
    num_profitable_windows: int | None
    mean_return_pct: Decimal | None
    stddev_return_pct: Decimal | None
    best_window_return_pct: Decimal | None
    worst_window_return_pct: Decimal | None
    error_detail: str | None
    created_at: datetime
    completed_at: datetime | None


class WalkForwardRunDetailResponse(WalkForwardRunSummary):
    """A summary plus the per-window breakdown, ordered by `window_index`.

    Subclasses `WalkForwardRunSummary` rather than restating its twenty
    fields - the same precedent `BacktestRunDetailResponse` set, so the two
    can never drift into disagreeing about what a run's aggregates are.
    """

    windows: list[WalkForwardWindowResponse]


class ListWalkForwardRunsResponse(BaseModel):
    """Paginated listing envelope - `{items, limit, offset}`, the same shape
    and the same generic `items` key as every other listing in this
    codebase."""

    items: list[WalkForwardRunSummary]
    limit: int
    offset: int
