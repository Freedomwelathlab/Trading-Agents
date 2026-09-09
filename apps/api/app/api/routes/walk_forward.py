"""Walk-forward consistency runs over a strategy version - the Strategy
Lab's third surface (Phase 57).

**A NEW file, not an edit of routes/strategy_backtests.py.** That module is
Phase 55's and stays as it is. What it already got right is imported rather
than copied: the two ownership helpers it itself imports from
routes/strategies.py (`_load_owned_strategy`, `_load_version`), and its
`_risk_limits` / `_portfolio_limits` builders. Those two matter as much as
the authorization helpers do - every window here runs through
`run_strategy_backtest`, so limits built differently in this module would
mean a walk-forward window silently ran under different risk rules than the
plain backtest a caller compares it against. One definition, no drift.

**Two routers, one file**, exactly as routes/strategy_backtests.py splits
its own surfaces: the nested routes hang off `/strategies` because a
walk-forward run is always OF a specific version and the path should say so;
the by-id read hangs off `/walk-forward-runs` because a run has its own
identity and a caller holding an id should not need to know which strategy
and version it came from. Both carry the same router-level
`strategy:backtest` dependency - deliberately the SAME permission Phase 55
added rather than a new one, because a walk-forward run is n backtests and
nothing more: anyone who may run one may run several.

**Two-part authorization, unchanged from D071.** The permission says this
account may run backtests at all; the per-request ownership check says over
which strategies. `GET /walk-forward-runs/{id}` re-derives ownership by
walking run -> version -> strategy -> owner rather than trusting an id to be
unguessable.

**Only a VALIDATED version can be walk-forward tested** - 409 otherwise,
same message pattern and same reason as the backtest route: it is what makes
the signal evaluator's already-validated precondition true.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.routes.strategies import _load_owned_strategy, _load_version
from apps.api.app.api.routes.strategy_backtests import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    _portfolio_limits,
    _risk_limits,
)
from apps.api.app.api.schemas_walk_forward import (
    CreateWalkForwardRunRequest,
    ListWalkForwardRunsResponse,
    WalkForwardRunDetailResponse,
    WalkForwardRunSummary,
    WalkForwardWindowResponse,
)
from apps.api.app.auth.dependencies import get_current_user, require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.backtesting.walk_forward import run_walk_forward
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    Strategy,
    StrategyVersion,
    StrategyVersionStatus,
    User,
    WalkForwardRun,
    WalkForwardWindow,
)
from apps.api.app.marketdata.store import MarketDataStore

router = APIRouter(
    prefix="/strategies",
    tags=["walk-forward"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_BACKTEST))],
)
"""The nested surface: run a walk-forward test of one version, list that
version's runs."""

runs_router = APIRouter(
    prefix="/walk-forward-runs",
    tags=["walk-forward"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_BACKTEST))],
)
"""The by-id surface. A run is addressable on its own because the id is what
a POST response, a log line and a UI list row all carry."""

logger = get_logger(__name__)


def _run_summary(run: WalkForwardRun) -> WalkForwardRunSummary:
    return WalkForwardRunSummary(
        id=run.id,
        strategy_version_id=run.strategy_version_id,
        symbol=run.symbol,
        bar_interval=run.bar_interval,
        overall_start_date=run.overall_start_date,
        overall_end_date=run.overall_end_date,
        window_days=run.window_days,
        starting_cash=run.starting_cash,
        status=run.status,
        num_windows=run.num_windows,
        num_succeeded_windows=run.num_succeeded_windows,
        num_profitable_windows=run.num_profitable_windows,
        mean_return_pct=run.mean_return_pct,
        stddev_return_pct=run.stddev_return_pct,
        best_window_return_pct=run.best_window_return_pct,
        worst_window_return_pct=run.worst_window_return_pct,
        error_detail=run.error_detail,
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


async def _run_detail(
    session: AsyncSession, run: WalkForwardRun
) -> WalkForwardRunDetailResponse:
    """The summary plus this run's windows, in `window_index` order so a
    client never has to sort them itself.

    A FAILED run simply has no windows - or, when every window's own backtest
    failed, has all of them with their real failed `backtest_run_id`s. Both
    are the honest answer rather than a fabricated breakdown.
    """
    window_rows = (
        (
            await session.execute(
                select(WalkForwardWindow)
                .where(WalkForwardWindow.walk_forward_run_id == run.id)
                .order_by(WalkForwardWindow.window_index.asc())
            )
        )
        .scalars()
        .all()
    )
    return WalkForwardRunDetailResponse(
        **_run_summary(run).model_dump(),
        windows=[
            WalkForwardWindowResponse(
                id=row.id,
                window_index=row.window_index,
                start_date=row.start_date,
                end_date=row.end_date,
                backtest_run_id=row.backtest_run_id,
            )
            for row in window_rows
        ],
    )


@router.post(
    "/{strategy_id}/versions/{version_id}/walk-forward",
    response_model=WalkForwardRunDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_walk_forward_run(
    strategy_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: CreateWalkForwardRunRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> WalkForwardRunDetailResponse:
    """Slices the range into sequential windows, runs a real backtest over
    each, aggregates the succeeded ones, and returns the result WITH its
    window breakdown - the caller who just triggered it is the one person
    guaranteed to want to see which windows ran and how each did.

    Runs SYNCHRONOUSLY, to completion, inside this request - n backtests
    where `POST .../backtests` runs one, the same posture and for the same
    reason (no queue, no worker, nothing able to race the row). A long range
    with short windows is therefore a long request; `window_days` is the
    caller's own lever on that.

    A 201 does NOT mean the strategy is consistent, or even that the run
    succeeded: a FAILED walk-forward run (too few windows fit, or every
    window's backtest failed) is a real, persisted resource that comes back
    with `status: "failed"` and a real `error_detail`. Same deliberate choice
    the persisted backtest route made.
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

    run = await run_walk_forward(
        session=session,
        strategy_version=version,
        symbol=payload.symbol,
        bar_interval=payload.bar_interval,
        overall_start_date=payload.overall_start_date,
        overall_end_date=payload.overall_end_date,
        window_days=payload.window_days,
        starting_cash=payload.starting_cash,
        # The persisted bar store (D070) is the data source - never a live
        # vendor call, exactly as the single-backtest route requires.
        bar_provider=MarketDataStore(session),
        risk_limits=_risk_limits(settings),
        portfolio_limits=_portfolio_limits(settings),
        requested_by_user_id=current_user.id,
    )
    # The orchestrator already committed - both on success and on failure -
    # so there is deliberately no second commit here.

    logger.info(
        "walk_forward_run_requested",
        walk_forward_run_id=str(run.id),
        strategy_id=str(strategy_id),
        strategy_version_id=str(version_id),
        symbol=payload.symbol,
        window_days=payload.window_days,
        status=run.status.value,
        actor_user_id=str(current_user.id),
    )
    return await _run_detail(session, run)


@router.get(
    "/{strategy_id}/versions/{version_id}/walk-forward",
    response_model=ListWalkForwardRunsResponse,
)
async def list_walk_forward_runs(
    strategy_id: uuid.UUID,
    version_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListWalkForwardRunsResponse:
    """This version's walk-forward runs, NEWEST `created_at` first, as
    summaries.

    Newest-first and summaries-only for the reasons the backtest listing
    already gives: the run someone wants is almost always the one they just
    did, and a table of aggregates should not drag every run's window
    breakdown along with it. `id` is the tiebreaker that keeps `offset`
    paging deterministic when two runs share a timestamp.
    """
    await _load_owned_strategy(session, strategy_id, current_user)
    await _load_version(session, strategy_id, version_id)

    rows = (
        (
            await session.execute(
                select(WalkForwardRun)
                .where(WalkForwardRun.strategy_version_id == version_id)
                .order_by(WalkForwardRun.created_at.desc(), WalkForwardRun.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListWalkForwardRunsResponse(
        items=[_run_summary(row) for row in rows], limit=limit, offset=offset
    )


@runs_router.get("/{run_id}", response_model=WalkForwardRunDetailResponse)
async def get_walk_forward_run(
    run_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WalkForwardRunDetailResponse:
    """One walk-forward run with its windows, addressed by its own id.

    Ownership is re-derived here, never assumed from possession of the id:
    the run's `strategy_version_id` leads to a version, which leads to a
    strategy, which carries `owner_user_id`. One join does all three, so
    there is no ordering of events in which another user's run is fetched and
    then filtered out.

    404 when no run has this id, 403 when one does but the caller does not
    own the strategy behind it - the same order and the same two codes
    `_load_owned_strategy` and `GET /backtest-runs/{id}` use, for the same
    reason: a nonexistent id must not 403 merely because nobody can own a row
    that isn't there.
    """
    row = (
        await session.execute(
            select(WalkForwardRun, Strategy.owner_user_id)
            .join(StrategyVersion, StrategyVersion.id == WalkForwardRun.strategy_version_id)
            .join(Strategy, Strategy.id == StrategyVersion.strategy_id)
            .where(WalkForwardRun.id == run_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No walk-forward run with id {run_id}.")

    run, owner_user_id = row
    if owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail=f"Walk-forward run {run_id} is not yours.")

    return await _run_detail(session, run)
