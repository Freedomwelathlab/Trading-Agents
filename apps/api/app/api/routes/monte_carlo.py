"""Monte Carlo resampling of a persisted backtest - the Strategy Lab's
fourth surface (Phase 57).

**A NEW file, not an edit of routes/strategy_backtests.py.** That module is
Phase 55's and stays as it is. What IS shared is the authorization rule:
these routes reuse `Permission.STRATEGY_BACKTEST` rather than introducing a
fifth permission, because "may run backtests of your own strategies" and
"may resample the results of your own backtests" are not two things anyone
would grant separately - a resampling reads exactly the rows a backtest
just wrote, and holding one permission without the other would authorize
nothing useful.

**Two routers, one file**, exactly as strategy_backtests.py splits its
own. The nested routes hang off `/backtest-runs/{id}/monte-carlo` because a
resampling is always OF one specific backtest run and the path should say
so; the by-id read hangs off `/monte-carlo-runs` because a Monte Carlo run
has its own identity and a caller holding its id should not need to also
know which backtest it came from.

**Ownership is re-derived on every request, one level deeper than Phase
55's.** `GET /backtest-runs/{id}` walks run -> version -> strategy ->
owner; `GET /monte-carlo-runs/{id}` walks mc run -> backtest run ->
version -> strategy -> owner. Both are single joins evaluated before
anything is read, so there is no ordering of events in which another
user's row is fetched and then filtered out. Holding an unguessable id is
never authorization here.

**Only a SUCCEEDED backtest run can be resampled** - 409 otherwise. A
FAILED run produced no trades at all, so resampling one would mean
bootstrapping from an empty sample; a run that never happened cannot be
asked what might have varied about it.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.schemas_monte_carlo import (
    CreateMonteCarloRunRequest,
    ListMonteCarloRunsResponse,
    MonteCarloRunResponse,
)
from apps.api.app.auth.dependencies import get_current_user, require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.backtesting.monte_carlo import run_monte_carlo
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    BacktestRun,
    BacktestRunStatus,
    MonteCarloRun,
    Strategy,
    StrategyVersion,
    User,
)

router = APIRouter(
    prefix="/backtest-runs",
    tags=["monte-carlo"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_BACKTEST))],
)
"""The nested surface: resample one backtest run, list that run's
resamplings. Same prefix as strategy_backtests.py's `runs_router` and the
same permission - the paths do not collide, since every route here carries
the `/monte-carlo` segment."""

runs_router = APIRouter(
    prefix="/monte-carlo-runs",
    tags=["monte-carlo"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_BACKTEST))],
)
"""The by-id surface, for the same reason `/backtest-runs` has one: the id
is what a POST response, a log line and a UI list row all carry."""

logger = get_logger(__name__)

DEFAULT_LIST_LIMIT = 50
"""The one pagination convention this codebase has - same default and
ceiling as every other listing (D030)."""
MAX_LIST_LIMIT = 500


def _response(run: MonteCarloRun) -> MonteCarloRunResponse:
    return MonteCarloRunResponse(
        id=run.id,
        backtest_run_id=run.backtest_run_id,
        num_simulations=run.num_simulations,
        random_seed=run.random_seed,
        status=run.status,
        num_trades_resampled=run.num_trades_resampled,
        median_final_equity=run.median_final_equity,
        p5_final_equity=run.p5_final_equity,
        p95_final_equity=run.p95_final_equity,
        median_max_drawdown_pct=run.median_max_drawdown_pct,
        p5_max_drawdown_pct=run.p5_max_drawdown_pct,
        p95_max_drawdown_pct=run.p95_max_drawdown_pct,
        probability_of_ruin_pct=run.probability_of_ruin_pct,
        error_detail=run.error_detail,
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


async def _load_owned_backtest_run(
    session: AsyncSession, backtest_run_id: uuid.UUID, current_user: User
) -> BacktestRun:
    """The backtest run behind `backtest_run_id`, or 404/403.

    This MIRRORS the join `routes/strategy_backtests.py`'s
    `get_backtest_run` performs - run -> version -> strategy ->
    `owner_user_id` - deliberately reproducing its rule rather than
    inventing a second one. It is written out here rather than imported
    because over there the lookup lives inline inside a route handler, and
    extracting it would mean editing Phase 55's module.

    404 before 403, the same order `_load_owned_strategy` uses: a
    nonexistent id must not answer 403 merely because nobody can own a row
    that isn't there.
    """
    row = (
        await session.execute(
            select(BacktestRun, Strategy.owner_user_id)
            .join(StrategyVersion, StrategyVersion.id == BacktestRun.strategy_version_id)
            .join(Strategy, Strategy.id == StrategyVersion.strategy_id)
            .where(BacktestRun.id == backtest_run_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"No backtest run with id {backtest_run_id}."
        )

    backtest_run, owner_user_id = row
    if owner_user_id != current_user.id:
        raise HTTPException(
            status_code=403, detail=f"Backtest run {backtest_run_id} is not yours."
        )
    return backtest_run


@router.post(
    "/{backtest_run_id}/monte-carlo",
    response_model=MonteCarloRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_monte_carlo_run(
    backtest_run_id: uuid.UUID,
    payload: CreateMonteCarloRunRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MonteCarloRunResponse:
    """Resamples the backtest run's trade returns and returns the finished
    row.

    409 when the source run is not `succeeded`, naming its actual status:
    only a successful run has trades, and an empty sample has no
    distribution to describe.

    Runs SYNCHRONOUSLY to completion inside this request - the posture
    every compute path in this codebase takes (D070, D072). A 201 does not
    mean the resampling succeeded: too few trades produces a real,
    persisted `status: "failed"` row with a real `error_detail`, exactly as
    a backtest over a data gap does.
    """
    backtest_run = await _load_owned_backtest_run(session, backtest_run_id, current_user)

    if backtest_run.status is not BacktestRunStatus.SUCCEEDED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"BACKTEST_NOT_SUCCEEDED: backtest run {backtest_run_id} is "
                f"{backtest_run.status.value}. Only a succeeded run has trades to resample."
            ),
        )

    run = await run_monte_carlo(
        session=session,
        backtest_run=backtest_run,
        num_simulations=payload.num_simulations,
        requested_by_user_id=current_user.id,
        # Never client-supplied: the seed is generated per run and
        # persisted, which makes the result reproducible without making it
        # steerable by whoever asked for it.
        seed=None,
    )
    # The engine already committed, on both paths - no second commit here.

    logger.info(
        "monte_carlo_run_requested",
        monte_carlo_run_id=str(run.id),
        backtest_run_id=str(backtest_run_id),
        num_simulations=payload.num_simulations,
        status=run.status.value,
        actor_user_id=str(current_user.id),
    )
    return _response(run)


@router.get("/{backtest_run_id}/monte-carlo", response_model=ListMonteCarloRunsResponse)
async def list_monte_carlo_runs(
    backtest_run_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListMonteCarloRunsResponse:
    """This backtest run's resamplings, NEWEST `created_at` first.

    A run may reasonably be analyzed more than once - at a different
    `num_simulations`, or simply again - so this is a real collection
    rather than a single row, and newest-first for the reason the backtest
    listing gives: the one someone wants is almost always the one they just
    did. `id` is the tiebreaker that keeps `offset` paging deterministic
    when two runs share a timestamp.
    """
    await _load_owned_backtest_run(session, backtest_run_id, current_user)

    rows = (
        (
            await session.execute(
                select(MonteCarloRun)
                .where(MonteCarloRun.backtest_run_id == backtest_run_id)
                .order_by(MonteCarloRun.created_at.desc(), MonteCarloRun.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListMonteCarloRunsResponse(
        items=[_response(row) for row in rows], limit=limit, offset=offset
    )


@runs_router.get("/{monte_carlo_run_id}", response_model=MonteCarloRunResponse)
async def get_monte_carlo_run(
    monte_carlo_run_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MonteCarloRunResponse:
    """One resampling run, addressed by its own id.

    The ownership chain is the backtest one plus a hop: mc run -> backtest
    run -> version -> strategy -> owner, all in a single join. 404 when no
    row has this id, 403 when one does but the caller does not own the
    strategy behind it - the same order and the same two codes every other
    route in the Strategy Lab uses.
    """
    row = (
        await session.execute(
            select(MonteCarloRun, Strategy.owner_user_id)
            .join(BacktestRun, BacktestRun.id == MonteCarloRun.backtest_run_id)
            .join(StrategyVersion, StrategyVersion.id == BacktestRun.strategy_version_id)
            .join(Strategy, Strategy.id == StrategyVersion.strategy_id)
            .where(MonteCarloRun.id == monte_carlo_run_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"No Monte Carlo run with id {monte_carlo_run_id}."
        )

    run, owner_user_id = row
    if owner_user_id != current_user.id:
        raise HTTPException(
            status_code=403, detail=f"Monte Carlo run {monte_carlo_run_id} is not yours."
        )

    return _response(run)
