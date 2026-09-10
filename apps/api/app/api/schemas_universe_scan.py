"""DTOs for the Phase 60 universe-scan routes.

Every money/percentage field is `Decimal`, never `float` - the project-wide
rule for anything financial, stated in schemas_strategy_backtests.py and
backtesting/models.py before it.

The summary/detail split is the same one `BacktestRunSummary` vs
`BacktestRunDetailResponse` and `WalkForwardRunSummary` vs its detail
established, for the same reason: a listing of fifty scans should not carry
fifty per-symbol breakdowns to render a table of four numbers per row. The
detail shape stays deliberately thin here too - a result row carries its
`backtest_run_id` and the four headline metrics, so a client that wants one
symbol's full equity curve and trade list fetches
`GET /backtest-runs/{id}` for exactly the symbol it cares about instead of
receiving every symbol's curve inline.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from apps.api.app.db.models import UniverseScanStatus


class CreateUniverseScanRequest(BaseModel):
    """The universe, the window and the capital. The strategy version is
    named by the path and supplies every RULE; this body carries nothing
    about the strategy itself.

    `symbols` omitted (or explicitly `null`) means "scan every symbol
    ingested at this interval" - the universe is whatever has actually been
    backfilled, which is the only universe a backtest could run over anyway.
    A non-empty list scans exactly those symbols, normalized (trimmed,
    upper-cased, de-duplicated) by the orchestrator.

    There is deliberately NO `min_length=1` on the list. An explicitly passed
    `[]` is a caller who meant something - most likely "scan everything" -
    and pydantic's generic "List should have at least 1 item" would not tell
    them which field to change or how. The route answers that case with a 422
    naming both options instead, which is worth one hand-written check.

    `bar_interval` is a `Literal["1d"]` for the reason
    `CreateBacktestRunRequest` gives: '1d' is the only interval anything
    ingests or evaluates today, so a 422 here says the actual thing where a
    free string would produce a scan that silently finds no bars for any
    symbol.
    """

    symbols: list[str] | None = None
    bar_interval: Literal["1d"] = "1d"
    start_date: date
    end_date: date
    starting_cash: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def _validate_range(self) -> "CreateUniverseScanRequest":
        """Same rule and wording as `CreateBacktestRunRequest` and
        `CreateWalkForwardRunRequest`: a range that does not move forward is
        a malformed request, not an empty result."""
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        return self


class UniverseScanResultResponse(BaseModel):
    """One symbol's outcome within a scan, plus its rank among the symbols
    that actually produced a result.

    `rank` is 1-indexed over the SUCCEEDED results by `total_return_pct`
    descending, and is computed on read (see
    `backtesting/universe_scan.py::order_and_rank_results`) rather than
    stored - the same choice D076's leaderboard makes.

    `rank` is `null` for a failed symbol. It was not measured and did not
    come last; ranking it at all would assert an ordering between a real
    result and a missing one. Its metrics are `null` too, never `0`
    (docs/TRADING_SAFETY.md's no-fabrication rule), and `error_detail` is
    what says why - most often that no bars are ingested for that symbol over
    this window.

    `backtest_run_id` is a live handle: this symbol's full equity curve and
    trade list are one `GET /backtest-runs/{id}` away, which is why none of
    it is duplicated here.
    """

    id: uuid.UUID
    symbol: str
    backtest_run_id: uuid.UUID
    status: str
    total_return_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    win_rate_pct: Decimal | None
    num_trades: int | None
    error_detail: str | None
    rank: int | None


class UniverseScanSummary(BaseModel):
    """One scan's identity, inputs and counts - no per-symbol list.

    `requested_symbols` is the EXACT list the caller asked for, normalized,
    and is `null` when `scan_mode` is `"all_ingested"` (the caller named
    none). What was actually scanned is `num_symbols`, and the detail
    response enumerates it - keeping the two separate is what makes any
    difference between them visible.

    Every count is optional because a FAILED scan computed none of them; a
    null always means "not counted", never 0. `num_symbols` counts every
    symbol ATTEMPTED, including any whose own backtest failed;
    `num_succeeded` is how many produced a result at all; `num_qualified`
    counts the succeeded ones with `total_return_pct > 0` - "profitable over
    this exact window", a deliberately simple and stated first-pass filter,
    not a recommendation and not a risk-adjusted judgement.
    """

    id: uuid.UUID
    strategy_version_id: uuid.UUID
    bar_interval: str
    start_date: date
    end_date: date
    starting_cash: Decimal
    scan_mode: str
    requested_symbols: list[str] | None
    status: UniverseScanStatus
    num_symbols: int | None
    num_succeeded: int | None
    num_qualified: int | None
    error_detail: str | None
    created_at: datetime
    completed_at: datetime | None


class UniverseScanDetailResponse(UniverseScanSummary):
    """A summary plus the per-symbol breakdown, already ordered: ranked
    results best-first, then every failed symbol (each `rank: null`) by
    symbol.

    Ordered server-side so every client renders the same ranking - the order
    IS the answer here, and leaving it to be re-sorted per client is how two
    UIs end up disagreeing about which symbol came first.

    Subclasses `UniverseScanSummary` rather than restating its fifteen
    fields - the same precedent `BacktestRunDetailResponse` and
    `WalkForwardRunDetailResponse` set, so the two can never drift into
    disagreeing about what a scan's counts are.
    """

    results: list[UniverseScanResultResponse]


class ListUniverseScansResponse(BaseModel):
    """Paginated listing envelope - `{items, limit, offset}`, the same shape
    and the same generic `items` key as every other listing in this
    codebase."""

    items: list[UniverseScanSummary]
    limit: int
    offset: int
