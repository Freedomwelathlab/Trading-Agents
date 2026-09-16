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

from pydantic import BaseModel, Field, model_validator

from apps.api.app.db.models import BacktestRunStatus
from apps.api.app.marketdata.bar_provider import BarInterval


class CreateBacktestRunRequest(BaseModel):
    """What to run the strategy version over. The version itself is named
    by the path, and everything about the RULES comes from it - this body
    carries only the market and the window.

    `bar_interval` is the shared `BarInterval` vocabulary
    (marketdata/bar_provider.py), not a free string: the underlying column
    is a plain string, so "1 day" and "1d" would otherwise be two intervals
    that never match each other on read. Phase 70 widened this from
    `Literal["1d"]` - requesting an interval with no ingested bars is a
    DATA question, answered by a persisted FAILED run naming the missing
    range, not a 422 on the interval itself.
    """

    symbol: str = Field(min_length=1, max_length=32)
    bar_interval: BarInterval = "1d"
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

    fee_bps: Decimal | None = None
    """Phase 70 (D088). The cost assumptions this run was executed under,
    and what they came to. Four nullable fields rather than a nested object,
    matching every other metric on this model.

    NULL here does NOT mean zero. It means the run predates cost modelling
    and its returns are frictionless - a materially different claim, and one
    a reader must be able to tell apart from "costed, and the costs were
    nil". A client rendering these must not substitute 0 for a null, for the
    same reason it must not render a null `win_rate_pct` as 0%."""
    slippage_bps: Decimal | None = None
    total_fees: Decimal | None = None
    total_slippage: Decimal | None = None


class BacktestRunDetailResponse(BacktestRunSummary):
    """A summary plus the full equity curve and trade list.

    Subclasses `BacktestRunSummary` rather than restating its fields, so
    the two can never drift into disagreeing about what a run's metrics
    are. Returned by the POST that creates a run - the caller who just
    triggered it wants to see it immediately - and by
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


class BacktestHistoryEntry(BacktestRunSummary):
    """One run in the cross-strategy history, carrying the two names a
    reader needs to tell runs apart (Phase 71, D089).

    `BacktestRunSummary` alone identifies a run only by
    `strategy_version_id` - a uuid, which tells a human nothing. The
    history view lists runs from MANY strategies side by side, so the
    strategy's name and the version number are joined in rather than left
    for the client to resolve with one follow-up request per row.
    """

    strategy_id: uuid.UUID
    strategy_name: str
    version_number: int


class ListBacktestHistoryResponse(BaseModel):
    """`{items, limit, offset}`, the same envelope as every other listing
    in this codebase."""

    items: list[BacktestHistoryEntry]
    limit: int
    offset: int
