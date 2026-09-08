"""DTOs for the Phase 55 backtest routes.

Every money/quantity/percentage field is `Decimal`, never `float` - the
project-wide rule for anything financial, and the same one
backtesting/models.py states for D025's v1 shapes.

The listing/detail split mirrors `StrategyVersionSummary` vs
`StrategyVersionResponse` exactly: a summary carries the run's metrics and
nothing else, while the full equity curve and trade list come back only
from a detail request. A strategy version with fifty runs would otherwise
return fifty multi-hundred-point curves to render a table of five numbers
per row.

These are deliberately NOT reused from backtesting/models.py. That module
is D025's v1 request/response vocabulary and stays untouched; its
`BacktestResult` describes a computed, unsaved value with no id, no status
and no failure representation, which is a different thing from a persisted
run that may have failed.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from apps.api.app.db.models import BacktestRunStatus


class CreateBacktestRunRequest(BaseModel):
    """What to run the strategy version over. The version itself is named
    by the path, and everything about the RULES comes from it - this body
    carries only the market and the window.

    `bar_interval` is a `Literal["1d"]` rather than a free string because
    '1d' is the only interval anything ingests or evaluates today
    (D070/Phase 54's indicator vocabulary). Accepting '1h' here would
    produce a run that silently finds no bars and fails, where a 422 says
    the actual thing.
    """

    symbol: str = Field(min_length=1, max_length=32)
    bar_interval: Literal["1d"] = "1d"
    start_date: date
    end_date: date
    starting_cash: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def _validate_range(self) -> "CreateBacktestRunRequest":
        """Same rule and same wording as D025's `BacktestRequest`: a
        one-day window has no room for an entry and its exit, so a range
        that does not move forward is a malformed request rather than an
        empty result."""
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self


class BacktestEquityPointResponse(BaseModel):
    """One day's mark-to-market equity. There is one of these per BAR in
    the window, not per calendar day - weekends and market holidays have no
    bar and therefore no point, and one is never interpolated."""

    date: date
    equity: Decimal


class BacktestTradeResponse(BaseModel):
    """One completed round trip. `side` is always "buy" in this phase - the
    engine only ever opens long (see engine_v2's `_persist_children`), a
    documented scope limit rather than a missing value.

    `exit_date` / `exit_price` / `return_pct` are optional in the schema
    because the column is, leaving room for a position still open at the end
    of a window; nothing this phase writes leaves them null."""

    side: str
    entry_date: date
    entry_price: Decimal
    exit_date: date | None
    exit_price: Decimal | None
    quantity: Decimal
    return_pct: Decimal | None


class BacktestRunSummary(BaseModel):
    """One run's identity, inputs and metrics - no curve, no trades.

    Every metric is optional because a FAILED run computed none of them. A
    null here means "not computed", never 0: reporting a 0% return for a run
    that never produced an equity curve would be a fabricated figure
    (docs/TRADING_SAFETY.md). `error_detail` is what says why, and it is
    null on a successful run for the same reason.
    """

    id: uuid.UUID
    strategy_version_id: uuid.UUID
    symbol: str
    bar_interval: str
    start_date: date
    end_date: date
    starting_cash: Decimal
    status: BacktestRunStatus
    final_equity: Decimal | None
    total_return_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    win_rate_pct: Decimal | None
    num_trades: int | None
    error_detail: str | None
    created_at: datetime
    completed_at: datetime | None


class BacktestRunDetailResponse(BacktestRunSummary):
    """A summary plus the full equity curve and trade list.

    Subclasses `BacktestRunSummary` rather than restating its fourteen
    fields, so the two can never drift into disagreeing about what a run's
    metrics are. Returned by the POST that creates a run - the caller who
    just triggered it wants to see it immediately - and by
    `GET /backtest-runs/{id}`.
    """

    equity_curve: list[BacktestEquityPointResponse]
    trades: list[BacktestTradeResponse]


class ListBacktestRunsResponse(BaseModel):
    """Paginated listing envelope - `{items, limit, offset}`, the same shape
    as `ListStrategiesResponse` and every other listing in this codebase.
    `items` rather than `runs`, for the reason `ListStrategiesResponse`
    already gives: one generic key across the Strategy Lab's resources
    rather than a differently-named list per resource."""

    items: list[BacktestRunSummary]
    limit: int
    offset: int
