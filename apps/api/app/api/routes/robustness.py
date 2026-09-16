"""Robustness (parameter-sensitivity) runs over a strategy version - the
Strategy Lab's fourth surface (Phase 58).

**A NEW file, not an edit of routes/strategy_backtests.py or
routes/walk_forward.py.** Those modules are Phase 55's and Phase 57's and
stay as they are. What they already got right is imported rather than
copied: the two ownership helpers routes/strategies.py owns
(`_load_owned_strategy`, `_load_version`), the pagination constants, and the
`_risk_limits` / `_portfolio_limits` builders. The limits matter as much as
the authorization helpers - every replay here runs through engine_v2's own
helpers, so limits built differently in this module would mean a
perturbation silently ran under different risk rules than the plain backtest
a caller compares it against. One definition, no drift.

**Two routers, one file**, exactly as the two sibling surfaces split their
own: the nested routes hang off `/strategies` because a robustness run is
always OF a specific version and the path should say so; the by-id read
hangs off `/robustness-runs` because a run has its own identity and a caller
holding an id should not need to know which strategy and version it came
from. Both carry the same router-level `strategy:backtest` dependency -
deliberately the SAME permission Phase 55 added rather than a new one, for
the reason D074 already gives for walk-forward and Monte Carlo both reusing
it: this is another analysis OF a backtest, not a new class of capability.
Anyone who may replay a strategy may replay it with one number nudged.

**Two-part authorization, unchanged from D071.** The permission says this
account may run backtests at all; the per-request ownership check says over
which strategies. `GET /robustness-runs/{id}` re-derives ownership by walking
run -> version -> strategy -> owner rather than trusting an id to be
unguessable.

**Only a VALIDATED version can be robustness tested** - 409 otherwise, same
message pattern and same reason as the backtest and walk-forward routes: it
is what makes the signal evaluator's already-validated precondition true,
for the baseline and for every perturbed variant of it alike.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.routes.strategies import _load_owned_strategy, _load_version
from apps.api.app.api.routes.strategy_backtests import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    _cost_model,
    _portfolio_limits,
    _quantity_precision,
    _risk_limits,
)
from apps.api.app.api.schemas_robustness import (
    CreateRobustnessRunRequest,
    ListRobustnessRunsResponse,
    RobustnessPerturbationResultResponse,
    RobustnessRunDetailResponse,
    RobustnessRunSummary,
)
from apps.api.app.auth.dependencies import get_current_user, require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.backtesting.robustness import run_robustness_test
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    RobustnessPerturbationResult,
    RobustnessRun,
    Strategy,
    StrategyVersion,
    StrategyVersionStatus,
    User,
)
from apps.api.app.marketdata.store import MarketDataStore

router = APIRouter(
    prefix="/strategies",
    tags=["robustness"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_BACKTEST))],
)
"""The nested surface: run a robustness test of one version, list that
version's runs."""

runs_router = APIRouter(
    prefix="/robustness-runs",
    tags=["robustness"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_BACKTEST))],
)
"""The by-id surface. A run is addressable on its own because the id is what
a POST response, a log line and a UI list row all carry."""

logger = get_logger(__name__)


def _run_summary(run: RobustnessRun) -> RobustnessRunSummary:
    return RobustnessRunSummary(
        id=run.id,
        strategy_version_id=run.strategy_version_id,
        symbol=run.symbol,
        bar_interval=run.bar_interval,
        start_date=run.start_date,
        end_date=run.end_date,
        starting_cash=run.starting_cash,
        magnitude_pct=run.magnitude_pct,
        status=run.status,
        baseline_return_pct=run.baseline_return_pct,
        baseline_max_drawdown_pct=run.baseline_max_drawdown_pct,
        num_perturbations=run.num_perturbations,
        num_succeeded_perturbations=run.num_succeeded_perturbations,
        mean_perturbed_return_pct=run.mean_perturbed_return_pct,
        stddev_perturbed_return_pct=run.stddev_perturbed_return_pct,
        max_return_deviation_pct=run.max_return_deviation_pct,
        error_detail=run.error_detail,
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


async def _run_detail(
    session: AsyncSession, run: RobustnessRun
) -> RobustnessRunDetailResponse:
    """The summary plus this run's perturbations, ordered by
    `(parameter_path, direction)` so a client never has to sort them itself
    and two reads of the same run always come back in the same order.

    That pair is the ordering rather than an index column because a
    perturbation has no natural sequence - it is identified by WHICH
    parameter moved and WHICH WAY, and grouping the two directions of one
    parameter together is how anyone reads this table. Insertion order would
    not be stable across reads, and a random `id` would be arbitrary.

    A FAILED run simply has no perturbations - or, when the baseline ran and
    every variant fell over, has all of them carrying their own real
    `error_detail`. Both are the honest answer rather than a fabricated
    breakdown.
    """
    perturbation_rows = (
        (
            await session.execute(
                select(RobustnessPerturbationResult)
                .where(RobustnessPerturbationResult.robustness_run_id == run.id)
                .order_by(
                    RobustnessPerturbationResult.parameter_path.asc(),
                    RobustnessPerturbationResult.direction.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return RobustnessRunDetailResponse(
        **_run_summary(run).model_dump(),
        perturbations=[
            RobustnessPerturbationResultResponse(
                id=row.id,
                parameter_path=row.parameter_path,
                direction=row.direction,
                original_value=row.original_value,
                perturbed_value=row.perturbed_value,
                clamped=row.clamped,
                status=row.status,
                total_return_pct=row.total_return_pct,
                max_drawdown_pct=row.max_drawdown_pct,
                error_detail=row.error_detail,
            )
            for row in perturbation_rows
        ],
    )


@router.post(
    "/{strategy_id}/versions/{version_id}/robustness",
    response_model=RobustnessRunDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_robustness_run(
    strategy_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: CreateRobustnessRunRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RobustnessRunDetailResponse:
    """Replays the version's definition unchanged, then once per parameter
    nudged +/-`magnitude_pct`, and returns the result WITH its per-parameter
    breakdown - the caller who just triggered it is the one person guaranteed
    to want to see which parameters were moved and what each did.

    Runs SYNCHRONOUSLY, to completion, inside this request - one replay per
    perturbation plus one for the baseline, where `POST .../backtests` runs
    one, the same posture and for the same reason (no queue, no worker,
    nothing able to race the row). A definition with many parameters over a
    long window is therefore a long request.

    A 201 does NOT mean the strategy is robust, or even that the run
    succeeded: a FAILED robustness run (nothing numeric to perturb, or a
    baseline that could not be replayed) is a real, persisted resource that
    comes back with `status: "failed"` and a real `error_detail`. Same
    deliberate choice the persisted backtest and walk-forward routes made.
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

    run = await run_robustness_test(
        session=session,
        strategy_version=version,
        symbol=payload.symbol,
        bar_interval=payload.bar_interval,
        start_date=payload.start_date,
        end_date=payload.end_date,
        starting_cash=payload.starting_cash,
        magnitude_pct=payload.magnitude_pct,
        # The persisted bar store (D070) is the data source - never a live
        # vendor call, exactly as the single-backtest route requires.
        bar_provider=MarketDataStore(session),
        risk_limits=_risk_limits(settings),
        portfolio_limits=_portfolio_limits(settings),
        cost_model=_cost_model(settings),
        quantity_precision=_quantity_precision(payload.symbol),
        requested_by_user_id=current_user.id,
    )
    # The orchestrator already committed - both on success and on failure -
    # so there is deliberately no second commit here.

    logger.info(
        "robustness_run_requested",
        robustness_run_id=str(run.id),
        strategy_id=str(strategy_id),
        strategy_version_id=str(version_id),
        symbol=payload.symbol,
        magnitude_pct=str(payload.magnitude_pct),
        status=run.status.value,
        actor_user_id=str(current_user.id),
    )
    return await _run_detail(session, run)


@router.get(
    "/{strategy_id}/versions/{version_id}/robustness",
    response_model=ListRobustnessRunsResponse,
)
async def list_robustness_runs(
    strategy_id: uuid.UUID,
    version_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListRobustnessRunsResponse:
    """This version's robustness runs, NEWEST `created_at` first, as
    summaries.

    Newest-first and summaries-only for the reasons the backtest and
    walk-forward listings already give: the run someone wants is almost
    always the one they just did, and a table of aggregates should not drag
    every run's per-parameter breakdown along with it. `id` is the tiebreaker
    that keeps `offset` paging deterministic when two runs share a timestamp.
    """
    await _load_owned_strategy(session, strategy_id, current_user)
    await _load_version(session, strategy_id, version_id)

    rows = (
        (
            await session.execute(
                select(RobustnessRun)
                .where(RobustnessRun.strategy_version_id == version_id)
                .order_by(RobustnessRun.created_at.desc(), RobustnessRun.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListRobustnessRunsResponse(
        items=[_run_summary(row) for row in rows], limit=limit, offset=offset
    )


@runs_router.get("/{run_id}", response_model=RobustnessRunDetailResponse)
async def get_robustness_run(
    run_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> RobustnessRunDetailResponse:
    """One robustness run with its perturbations, addressed by its own id.

    Ownership is re-derived here, never assumed from possession of the id:
    the run's `strategy_version_id` leads to a version, which leads to a
    strategy, which carries `owner_user_id`. One join does all three, so
    there is no ordering of events in which another user's run is fetched and
    then filtered out.

    404 when no run has this id, 403 when one does but the caller does not
    own the strategy behind it - the same order and the same two codes
    `_load_owned_strategy`, `GET /backtest-runs/{id}` and
    `GET /walk-forward-runs/{id}` use, for the same reason: a nonexistent id
    must not 403 merely because nobody can own a row that isn't there.
    """
    row = (
        await session.execute(
            select(RobustnessRun, Strategy.owner_user_id)
            .join(StrategyVersion, StrategyVersion.id == RobustnessRun.strategy_version_id)
            .join(Strategy, Strategy.id == StrategyVersion.strategy_id)
            .where(RobustnessRun.id == run_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No robustness run with id {run_id}.")

    run, owner_user_id = row
    if owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail=f"Robustness run {run_id} is not yours.")

    return await _run_detail(session, run)
