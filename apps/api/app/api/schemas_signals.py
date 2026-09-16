"""DTOs for the Phase 61 signal routes.

`latest_close` is `Decimal`, never `float` - the project-wide rule for
anything financial, stated in schemas_strategy_backtests.py and
backtesting/models.py before it.

**There is no summary/detail split here**, unlike every other Strategy Lab
surface. A `BacktestRun` has an equity curve and a trade list to leave out of
a listing; a signal evaluation has no large child collection at all - its
whole content is a verdict, a handful of scalars and one small indicator map.
Splitting it would produce two shapes that are the same shape, and a client
would immediately have to re-fetch each row to see the explanation, which is
the single most useful field on it.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from apps.api.app.db.models import SignalDirection
from apps.api.app.marketdata.bar_provider import BarInterval


class CreateSignalEvaluationRequest(BaseModel):
    """Which symbols to evaluate this version against, and at what bar
    interval. The strategy version is named by the path and supplies every
    RULE; this body carries nothing about the strategy itself.

    **There is deliberately no date range.** A current signal is by definition
    computed from the LATEST bars the store holds - letting a caller name a
    window would turn this into a backtest with one bar, which
    `POST .../backtests` already does properly.

    `bar_interval` is the shared `BarInterval` vocabulary for the reason
    `CreateBacktestRunRequest` gives: one closed spelling of each
    interval, so the plain-string column underneath cannot end up
    holding two spellings that never match each other on read.

    The 50-symbol cap is the same order of magnitude as a universe scan's, but
    for a much cheaper unit of work: each symbol here is one small indexed
    read plus an in-memory evaluation, not a full replay. It exists so a
    synchronous request stays synchronous, not because the work is heavy.
    """

    symbols: list[str] = Field(min_length=1, max_length=50)
    bar_interval: BarInterval = "1d"


class SignalEvaluationResponse(BaseModel):
    """One persisted answer: what this version says to do for this symbol, as
    of the latest bar held, and why.

    `entry_rule_held` / `exit_rule_held` are THREE-VALUED - `true`, `false`, or
    `null` for "could not be evaluated at that bar". A client must not render
    `null` as "no": an indicator still inside its warmup is not the same fact
    as a condition that was checked and found absent.

    `as_of_bar_date` and `latest_close` are `null` only when zero bars were
    available; they are never back-filled with the request time or a
    last-known price (docs/TRADING_SAFETY.md's no-fabrication rule).

    `indicator_values` maps each declared indicator id to its value AT THAT
    BAR, as a STRING (`"105.50"`) so no Decimal is ever routed through a JSON
    float, or `null` where the indicator has no value yet.

    `explanation` always names the concrete numbers behind the verdict,
    including for HOLD and for insufficient data - the "never a black-box BUY"
    contract of spec section 25. It is the field a UI should show beside the
    signal, not one to hide behind a disclosure triangle.
    """

    id: uuid.UUID
    strategy_version_id: uuid.UUID
    symbol: str
    bar_interval: str
    as_of_bar_date: date | None
    latest_close: Decimal | None
    signal: SignalDirection
    entry_rule_held: bool | None
    exit_rule_held: bool | None
    insufficient_data: bool
    indicator_values: dict[str, str | None]
    explanation: str
    created_at: datetime


class SignalEvaluationBatchResponse(BaseModel):
    """The POST's response: one evaluation per REQUESTED symbol, in REQUEST
    ORDER.

    No `limit`/`offset` here, unlike every listing envelope in this codebase,
    and deliberately: the batch is exactly the symbols the caller asked for -
    there is nothing to page through and nothing was left out. Every item is
    already persisted, including the ones that say `insufficient_data: true`,
    which are answers rather than omissions.
    """

    items: list[SignalEvaluationResponse]


class ListSignalEvaluationsResponse(BaseModel):
    """Paginated listing envelope - `{items, limit, offset}`, the same shape
    and the same generic `items` key as every other listing in this
    codebase."""

    items: list[SignalEvaluationResponse]
    limit: int
    offset: int
