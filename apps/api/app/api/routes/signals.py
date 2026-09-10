"""Current-signal evaluation over a strategy version - the Strategy Lab's
present-tense surface (Phase 61).

**A NEW file, not an edit of routes/strategy_backtests.py, walk_forward.py or
universe_scan.py.** Those modules belong to Phases 55/57/60 and stay as they
are. What they already got right is imported rather than copied: the two
ownership helpers (`_load_owned_strategy`, `_load_version`) and the listing
bounds. Notably NOT imported are `_risk_limits` / `_portfolio_limits` - and
their absence is the point. Nothing on this surface simulates a fill, sizes a
position or touches capital, so there are no limits to build; a signal is a
PROPOSAL, and every real order still goes through the deterministic Risk
Engine on its own path.

**Two routers, one file**, the same split the three surfaces above use: the
nested routes hang off `/strategies` because an evaluation is always OF a
specific version and the path should say so; the by-id read hangs off
`/signal-evaluations` because a row has its own identity and a caller holding
an id should not need to know which strategy and version it came from.

**Both carry `strategy:signal`, a NEW permission - not `strategy:backtest`.**
This is the first surface since Phase 55 to add one rather than reuse it, and
the reason is stated in full on `Permission.STRATEGY_SIGNAL`: a backtest is a
historical what-if, a signal is a present-tense "what should happen now" and
is the input Phase 63's paper-trading runner will act on. A role that may
study a strategy's past must not automatically be able to ask what it says to
do right now.

**Two-part authorization, unchanged from D071.** The permission says this
account may ask for signals at all; the per-request ownership check says over
which strategies. `GET /signal-evaluations/{id}` re-derives ownership by
walking evaluation -> version -> strategy -> owner rather than trusting an id
to be unguessable.

**Only a VALIDATED version can be evaluated** - 409 otherwise, same message
pattern and same reason as the backtest, walk-forward and scan routes: it is
what makes the signal evaluator's already-validated precondition true.

**On demand, not scheduled.** Each POST evaluates and persists inside the
request. There is no recurring job in this phase - Phase 63's deployed-strategy
runner is what will call this engine each cycle.
"""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.routes.strategies import _load_owned_strategy, _load_version
from apps.api.app.api.routes.strategy_backtests import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT
from apps.api.app.api.schemas_signals import (
    CreateSignalEvaluationRequest,
    ListSignalEvaluationsResponse,
    SignalEvaluationBatchResponse,
    SignalEvaluationResponse,
)
from apps.api.app.auth.dependencies import get_current_user, require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.backtesting.engine_v2 import _warmup_bar_count
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    SignalDirection,
    SignalEvaluation,
    Strategy,
    StrategyVersion,
    StrategyVersionStatus,
    User,
)
from apps.api.app.marketdata.store import MarketDataStore
from apps.api.app.signals.engine import evaluate_current_signal

router = APIRouter(
    prefix="/strategies",
    tags=["signals"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_SIGNAL))],
)
"""The nested surface: evaluate one version's current signal for a batch of
symbols, list that version's past evaluations."""

evaluations_router = APIRouter(
    prefix="/signal-evaluations",
    tags=["signals"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_SIGNAL))],
)
"""The by-id surface. An evaluation is addressable on its own because the id is
what a POST response, a log line and a UI list row all carry."""

logger = get_logger(__name__)

EVALUATION_BAR_BUFFER = 5
"""Bars fetched BEYOND `_warmup_bar_count(definition)`.

The warmup count is the minimum for the indicators to be defined at the
window's first bar; a crossing operator at the LATEST bar additionally needs
the bar before it to be fully evaluated, and RSI's `period + 1` boundary eats
into the same slack. Five bars is a deliberately cheap over-fetch - each one
is a row off an index already being read - chosen so a genuine crossing on the
newest bar is never missed for want of one row of context. It is not a warmup
formula of its own: `_warmup_bar_count` remains the single definition of that,
reused here rather than restated.
"""


def _normalize_symbol(symbol: str) -> str:
    """Trim and upper-case, the rule `schemas_watchlists.normalize_symbol` and
    `universe_scan.normalize_symbols` both apply, and for the same reason: the
    symbol persisted must be the exact string handed to the bar store, so
    "aapl" and "AAPL " cannot become two different records of one market. It
    is also what makes the `?symbol=` filter below match rows this route
    wrote.

    Deliberately NOT de-duplicating, which `normalize_symbols` does: the
    response contract here is one item per REQUESTED symbol in request order,
    and a scan's reason for de-duplicating (a repeated symbol would run a full
    backtest twice and double-count it in the aggregates) does not apply -
    there are no aggregates here, and a repeated symbol simply gets the same
    answer twice.
    """
    return symbol.strip().upper()


def _serialized_indicator_values(values: dict[str, Decimal | None]) -> dict[str, str | None]:
    """Decimals as STRINGS, `None` preserved. JSON numbers are IEEE floats and
    this codebase is Decimal end to end, so `"105.50"` is stored and returned
    rather than `105.5`; `null` means "this indicator had no value at that
    bar" and is never rewritten to 0."""
    return {
        name: (str(value) if value is not None else None) for name, value in values.items()
    }


def _response(row: SignalEvaluation) -> SignalEvaluationResponse:
    return SignalEvaluationResponse(
        id=row.id,
        strategy_version_id=row.strategy_version_id,
        symbol=row.symbol,
        bar_interval=row.bar_interval,
        as_of_bar_date=row.as_of_bar_date,
        latest_close=row.latest_close,
        signal=row.signal,
        entry_rule_held=row.entry_rule_held,
        exit_rule_held=row.exit_rule_held,
        insufficient_data=row.insufficient_data,
        indicator_values=row.indicator_values,
        explanation=row.explanation,
        created_at=row.created_at,
    )


@router.post(
    "/{strategy_id}/versions/{version_id}/signals",
    response_model=SignalEvaluationBatchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_signal_evaluations(
    strategy_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: CreateSignalEvaluationRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SignalEvaluationBatchResponse:
    """Evaluates this version against the LATEST ingested bars for each
    requested symbol, persists one row per symbol, and returns them in request
    order.

    Runs SYNCHRONOUSLY in-request, like every other job in this codebase - and
    far more comfortably than the ones that do so: at most 50 symbols, each
    one indexed read of a few dozen bars plus an in-memory rule evaluation. No
    replay, no fills, no capital modelling.

    A 201 does NOT mean any symbol produced a tradable signal. A symbol with
    no ingested bars, or with too few for its indicators, comes back as a real
    persisted row with `signal: "hold"`, `insufficient_data: true` and an
    explanation naming how many bars actually exist - the honest answer, kept
    rather than dropped, exactly as a FAILED per-symbol row is kept in a
    universe scan.

    Nothing here places, sizes or proposes an order to any broker. A signal is
    an assertion about rules and prices; every real trade still goes through
    the deterministic Risk Engine and the trade-submission permissions.
    """
    await _load_owned_strategy(session, strategy_id, current_user)
    version = await _load_version(session, strategy_id, version_id)

    if version.status is not StrategyVersionStatus.VALIDATED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"VERSION_NOT_VALIDATED: version {version.id} is {version.status.value}. "
                f"POST /strategies/{strategy_id}/versions/{version_id}/validate first."
            ),
        )

    definition = version.definition
    # One count for the whole batch: it depends only on the definition, which
    # is frozen for a validated version.
    count = _warmup_bar_count(definition) + EVALUATION_BAR_BUFFER
    # The persisted bar store (D070) is the data source - never a live vendor
    # call, exactly as every other Strategy Lab surface requires.
    store = MarketDataStore(session)

    rows: list[SignalEvaluation] = []
    for raw_symbol in payload.symbols:
        symbol = _normalize_symbol(raw_symbol)
        bars = await store.get_latest_bars(
            symbol, bar_interval=payload.bar_interval, count=count
        )
        current = evaluate_current_signal(bars, definition)
        row = SignalEvaluation(
            id=uuid.uuid4(),
            strategy_version_id=version.id,
            requested_by_user_id=current_user.id,
            symbol=symbol,
            bar_interval=payload.bar_interval,
            as_of_bar_date=current.as_of.date() if current.as_of is not None else None,
            latest_close=current.latest_close,
            signal=SignalDirection(current.signal.value),
            entry_rule_held=current.entry_rule_held,
            exit_rule_held=current.exit_rule_held,
            insufficient_data=current.insufficient_data,
            indicator_values=_serialized_indicator_values(current.indicator_values),
            explanation=current.explanation,
        )
        session.add(row)
        rows.append(row)

    await session.commit()
    for row in rows:
        await session.refresh(row)

    logger.info(
        "signal_evaluations_requested",
        strategy_id=str(strategy_id),
        strategy_version_id=str(version_id),
        bar_interval=payload.bar_interval,
        num_symbols=len(rows),
        num_insufficient_data=sum(1 for row in rows if row.insufficient_data),
        actor_user_id=str(current_user.id),
    )
    return SignalEvaluationBatchResponse(items=[_response(row) for row in rows])


@router.get(
    "/{strategy_id}/versions/{version_id}/signals",
    response_model=ListSignalEvaluationsResponse,
)
async def list_signal_evaluations(
    strategy_id: uuid.UUID,
    version_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    symbol: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListSignalEvaluationsResponse:
    """This version's signal evaluations, NEWEST `created_at` first,
    optionally for one symbol.

    Newest-first for the reason every other listing here gives, and more
    strongly: a current signal ages. The row someone wants is almost always
    the most recent one, and an older row is history rather than a stale
    answer to be corrected - the table is append-only, so re-evaluating a
    symbol adds a row instead of replacing one.

    Items are the FULL shape rather than summaries. A signal evaluation has no
    large child collection to leave out, and the `explanation` a summary would
    drop is the most useful field on it.

    `symbol` is normalized exactly as the POST normalizes what it writes, so a
    filter typed "aapl" finds the rows stored as "AAPL". `id` is the tiebreaker
    that keeps `offset` paging deterministic when two rows share a timestamp -
    which is likelier here than anywhere else, since one batch writes many rows
    in one transaction.
    """
    await _load_owned_strategy(session, strategy_id, current_user)
    await _load_version(session, strategy_id, version_id)

    query = select(SignalEvaluation).where(
        SignalEvaluation.strategy_version_id == version_id
    )
    if symbol is not None:
        query = query.where(SignalEvaluation.symbol == _normalize_symbol(symbol))

    rows = (
        (
            await session.execute(
                query.order_by(
                    SignalEvaluation.created_at.desc(), SignalEvaluation.id.desc()
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListSignalEvaluationsResponse(
        items=[_response(row) for row in rows], limit=limit, offset=offset
    )


@evaluations_router.get("/{evaluation_id}", response_model=SignalEvaluationResponse)
async def get_signal_evaluation(
    evaluation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SignalEvaluationResponse:
    """One signal evaluation, addressed by its own id.

    Ownership is re-derived here, never assumed from possession of the id: the
    row's `strategy_version_id` leads to a version, which leads to a strategy,
    which carries `owner_user_id`. One join does all three, so there is no
    ordering of events in which another user's signal is fetched and then
    filtered out.

    404 when no evaluation has this id, 403 when one does but the caller does
    not own the strategy behind it - the same order and the same two codes
    `_load_owned_strategy` and `GET /walk-forward-runs/{id}` use, for the same
    reason: a nonexistent id must not 403 merely because nobody can own a row
    that isn't there.
    """
    row = (
        await session.execute(
            select(SignalEvaluation, Strategy.owner_user_id)
            .join(
                StrategyVersion,
                StrategyVersion.id == SignalEvaluation.strategy_version_id,
            )
            .join(Strategy, Strategy.id == StrategyVersion.strategy_id)
            .where(SignalEvaluation.id == evaluation_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"No signal evaluation with id {evaluation_id}."
        )

    evaluation, owner_user_id = row
    if owner_user_id != current_user.id:
        raise HTTPException(
            status_code=403, detail=f"Signal evaluation {evaluation_id} is not yours."
        )

    return _response(evaluation)
