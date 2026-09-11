"""Strategy-deployment routes (Phase 63, D081) - put a validated
`StrategyVersion` on the scheduled paper-trading runner, behind a mandatory
human-approval gate.

Two routers, the same split the signal and universe-scan surfaces use:
- `router` (`/strategies/...`) - create a deployment for a version, list a
  version's deployments. The path says which version because a deployment
  is always OF one.
- `deployments_router` (`/deployments/...`) - read one deployment, run its
  lifecycle actions (approve / pause / resume / stop), list its runs and
  the signals its runs produced, (Phase 65, D083) report its actual vs.
  expected performance, and (Phase 66, D084) list its append-only drift
  checks. A deployment has its own id and a caller holding it should not
  need to know the version path.

Authorization is two-part, unchanged from D071:
- the router-level permission (`STRATEGY_DEPLOY` for everything except
  approval, which needs `STRATEGY_APPROVE_DEPLOYMENT` - and, for a `live`
  deployment, `STRATEGY_APPROVE_LIVE_DEPLOYMENT` as well, Phase 64/D082);
- a per-request ownership check - the deployment's version must belong to a
  strategy the caller owns (`_load_owned_strategy` / `_load_version`,
  imported from `routes/strategies.py`).

Nothing here starts trading, in either mode. Creating a deployment leaves it
`PENDING_APPROVAL`; only the explicit approval action moves it to `ACTIVE`,
and only then does the runner (if enabled at all) look at it - and for a
`live` deployment, "look at it" means writing a `SKIPPED_LIVE_TRADING_DISABLED`
run row and nothing else (see `deployments/runner.py`).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.routes.strategies import _load_owned_strategy, _load_version
from apps.api.app.api.routes.strategy_backtests import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT
from apps.api.app.api.schemas_deployments import (
    ApproveDeploymentRequest,
    CreateDeploymentRequest,
    DeploymentActualPerformanceResponse,
    DeploymentExpectedPerformanceResponse,
    DeploymentMonitoringResponse,
    DeploymentSignalResponse,
    DriftCheckResponse,
    ListDeploymentRunsResponse,
    ListDeploymentSignalsResponse,
    ListDeploymentsResponse,
    ListDriftChecksResponse,
    PauseDeploymentRequest,
    RoundTripResponse,
    StrategyDeploymentResponse,
    StrategyDeploymentRunResponse,
)
from apps.api.app.auth.dependencies import get_current_user, require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    SignalEvaluation,
    Strategy,
    StrategyDeployment,
    StrategyDeploymentRun,
    StrategyDriftCheck,
    StrategyVersion,
    User,
)
from apps.api.app.deployments.monitoring import build_deployment_monitoring
from apps.api.app.deployments.service import (
    DeploymentError,
    approve_deployment,
    create_deployment,
    pause_deployment,
    resume_deployment,
    stop_deployment,
)

logger = get_logger(__name__)

router = APIRouter(
    prefix="/strategies",
    tags=["deployments"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_DEPLOY))],
)

deployments_router = APIRouter(
    prefix="/deployments",
    tags=["deployments"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_DEPLOY))],
)

# Approval is the one action gated by the stricter, separate permission.
_approve_router = APIRouter(
    prefix="/deployments",
    tags=["deployments"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_APPROVE_DEPLOYMENT))],
)


def _response(row: StrategyDeployment) -> StrategyDeploymentResponse:
    return StrategyDeploymentResponse(
        id=row.id,
        strategy_version_id=row.strategy_version_id,
        broker_id=row.broker_id,
        mode=row.mode,
        status=row.status,
        symbols=list(row.symbols),
        bar_interval=row.bar_interval,
        requested_by_user_id=row.requested_by_user_id,
        approved_by_user_id=row.approved_by_user_id,
        approved_at=row.approved_at,
        paused_reason=row.paused_reason,
        stopped_at=row.stopped_at,
        last_evaluated_at=row.last_evaluated_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _run_response(row: StrategyDeploymentRun) -> StrategyDeploymentRunResponse:
    return StrategyDeploymentRunResponse(
        id=row.id,
        deployment_id=row.deployment_id,
        status=row.status,
        started_at=row.started_at,
        completed_at=row.completed_at,
        symbols_evaluated=row.symbols_evaluated,
        signals_actionable=row.signals_actionable,
        orders_submitted=row.orders_submitted,
        orders_filled=row.orders_filled,
        error_detail=row.error_detail,
        created_at=row.created_at,
    )


async def _load_owned_deployment(
    session: AsyncSession, deployment_id: uuid.UUID, current_user: User
) -> StrategyDeployment:
    """404 if no deployment has this id; 403 if one does but its strategy is
    not the caller's - ownership re-derived deployment -> version ->
    strategy -> owner, the same order and codes `_load_owned_strategy`
    uses."""
    row = (
        await session.execute(
            select(StrategyDeployment, Strategy.owner_user_id)
            .join(StrategyVersion, StrategyVersion.id == StrategyDeployment.strategy_version_id)
            .join(Strategy, Strategy.id == StrategyVersion.strategy_id)
            .where(StrategyDeployment.id == deployment_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No deployment with id {deployment_id}.")
    deployment, owner_user_id = row
    if owner_user_id != current_user.id:
        raise HTTPException(
            status_code=403, detail=f"Deployment {deployment_id} is not yours."
        )
    return deployment


@router.post(
    "/{strategy_id}/versions/{version_id}/deployments",
    response_model=StrategyDeploymentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_strategy_deployment(
    strategy_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: CreateDeploymentRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StrategyDeploymentResponse:
    """Create a deployment for this version. It is created `PENDING_APPROVAL`
    - the runner ignores it until a separate approval action (its own
    permission) moves it to `ACTIVE`. A 201 does NOT mean anything is
    trading."""
    await _load_owned_strategy(session, strategy_id, current_user)
    await _load_version(session, strategy_id, version_id)
    try:
        deployment = await create_deployment(
            session,
            strategy_version_id=version_id,
            broker_id=payload.broker_id,
            symbols=payload.symbols,
            bar_interval=payload.bar_interval,
            mode=payload.mode,
            requested_by_user_id=current_user.id,
        )
    except DeploymentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    await session.refresh(deployment)
    logger.info(
        "strategy_deployment_created",
        deployment_id=str(deployment.id),
        strategy_version_id=str(version_id),
        broker_id=str(payload.broker_id),
        num_symbols=len(deployment.symbols),
        actor_user_id=str(current_user.id),
    )
    return _response(deployment)


@router.get(
    "/{strategy_id}/versions/{version_id}/deployments",
    response_model=ListDeploymentsResponse,
)
async def list_strategy_deployments(
    strategy_id: uuid.UUID,
    version_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListDeploymentsResponse:
    await _load_owned_strategy(session, strategy_id, current_user)
    await _load_version(session, strategy_id, version_id)
    rows = (
        (
            await session.execute(
                select(StrategyDeployment)
                .where(StrategyDeployment.strategy_version_id == version_id)
                .order_by(StrategyDeployment.created_at.desc(), StrategyDeployment.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListDeploymentsResponse(
        items=[_response(r) for r in rows], limit=limit, offset=offset
    )


@deployments_router.get("/{deployment_id}", response_model=StrategyDeploymentResponse)
async def get_deployment(
    deployment_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StrategyDeploymentResponse:
    return _response(await _load_owned_deployment(session, deployment_id, current_user))


@_approve_router.post("/{deployment_id}/approve", response_model=StrategyDeploymentResponse)
async def approve_strategy_deployment(
    deployment_id: uuid.UUID,
    payload: ApproveDeploymentRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StrategyDeploymentResponse:
    """The mandatory human gate: `PENDING_APPROVAL` -> `ACTIVE`. Gated by
    `STRATEGY_APPROVE_DEPLOYMENT`, deliberately separate from
    `STRATEGY_DEPLOY` so an organization can require a second person.

    A `mode='live'` deployment additionally requires
    `STRATEGY_APPROVE_LIVE_DEPLOYMENT` (Phase 64, D082) - strictly more
    demanding than the paper case, the same shape `trade:submit:live` adds
    on top of `trade:submit:paper` for a single order (D058)."""
    deployment = await _load_owned_deployment(session, deployment_id, current_user)
    if deployment.mode == "live":
        role = current_user.role
        if role is None or Permission.STRATEGY_APPROVE_LIVE_DEPLOYMENT.value not in (
            role.permissions
        ):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Missing required permission: "
                    f"{Permission.STRATEGY_APPROVE_LIVE_DEPLOYMENT.value}"
                ),
            )
    try:
        await approve_deployment(deployment, approved_by_user_id=current_user.id)
    except DeploymentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    await session.refresh(deployment)
    logger.info(
        "strategy_deployment_approved",
        deployment_id=str(deployment_id),
        approver_user_id=str(current_user.id),
    )
    return _response(deployment)


async def _transition(
    session: AsyncSession,
    deployment: StrategyDeployment,
    coro,
    *,
    event: str,
    actor_user_id: uuid.UUID,
) -> StrategyDeploymentResponse:
    try:
        await coro
    except DeploymentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    await session.refresh(deployment)
    logger.info(event, deployment_id=str(deployment.id), actor_user_id=str(actor_user_id))
    return _response(deployment)


@deployments_router.post("/{deployment_id}/pause", response_model=StrategyDeploymentResponse)
async def pause_strategy_deployment(
    deployment_id: uuid.UUID,
    payload: PauseDeploymentRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StrategyDeploymentResponse:
    deployment = await _load_owned_deployment(session, deployment_id, current_user)
    return await _transition(
        session,
        deployment,
        pause_deployment(deployment, reason=payload.reason),
        event="strategy_deployment_paused",
        actor_user_id=current_user.id,
    )


@deployments_router.post("/{deployment_id}/resume", response_model=StrategyDeploymentResponse)
async def resume_strategy_deployment(
    deployment_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StrategyDeploymentResponse:
    deployment = await _load_owned_deployment(session, deployment_id, current_user)
    return await _transition(
        session,
        deployment,
        resume_deployment(deployment),
        event="strategy_deployment_resumed",
        actor_user_id=current_user.id,
    )


@deployments_router.post("/{deployment_id}/stop", response_model=StrategyDeploymentResponse)
async def stop_strategy_deployment(
    deployment_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StrategyDeploymentResponse:
    deployment = await _load_owned_deployment(session, deployment_id, current_user)
    return await _transition(
        session,
        deployment,
        stop_deployment(deployment),
        event="strategy_deployment_stopped",
        actor_user_id=current_user.id,
    )


@deployments_router.get(
    "/{deployment_id}/runs", response_model=ListDeploymentRunsResponse
)
async def list_deployment_runs(
    deployment_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListDeploymentRunsResponse:
    await _load_owned_deployment(session, deployment_id, current_user)
    rows = (
        (
            await session.execute(
                select(StrategyDeploymentRun)
                .where(StrategyDeploymentRun.deployment_id == deployment_id)
                .order_by(
                    StrategyDeploymentRun.created_at.desc(), StrategyDeploymentRun.id.desc()
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListDeploymentRunsResponse(
        items=[_run_response(r) for r in rows], limit=limit, offset=offset
    )


@deployments_router.get(
    "/{deployment_id}/signals", response_model=ListDeploymentSignalsResponse
)
async def list_deployment_signals(
    deployment_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListDeploymentSignalsResponse:
    """Every `SignalEvaluation` any of this deployment's runs produced,
    newest first - the deployment's own signal trail, joined through
    `strategy_deployment_runs`."""
    await _load_owned_deployment(session, deployment_id, current_user)
    rows = (
        (
            await session.execute(
                select(SignalEvaluation)
                .join(
                    StrategyDeploymentRun,
                    StrategyDeploymentRun.id == SignalEvaluation.deployment_run_id,
                )
                .where(StrategyDeploymentRun.deployment_id == deployment_id)
                .order_by(SignalEvaluation.created_at.desc(), SignalEvaluation.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListDeploymentSignalsResponse(
        items=[
            DeploymentSignalResponse(
                id=r.id,
                deployment_run_id=r.deployment_run_id,
                symbol=r.symbol,
                as_of_bar_date=r.as_of_bar_date,
                latest_close=r.latest_close,
                signal=r.signal,
                insufficient_data=r.insufficient_data,
                explanation=r.explanation,
                created_at=r.created_at,
            )
            for r in rows
        ],
        limit=limit,
        offset=offset,
    )


@deployments_router.get(
    "/{deployment_id}/monitoring", response_model=DeploymentMonitoringResponse
)
async def get_deployment_monitoring(
    deployment_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DeploymentMonitoringResponse:
    """Phase 65 (D083) - actual performance (built strictly from this
    deployment's own real orders/fills) side by side with expected
    performance (the strategy version's own latest real backtest), never
    blended. Read-only: places no orders, writes nothing."""
    deployment = await _load_owned_deployment(session, deployment_id, current_user)
    result = await build_deployment_monitoring(session, deployment)
    return DeploymentMonitoringResponse(
        deployment_id=result.deployment_id,
        as_of=result.as_of,
        actual=DeploymentActualPerformanceResponse(
            round_trips=[
                RoundTripResponse(
                    symbol=rt.symbol,
                    quantity=rt.quantity,
                    entry_price=rt.entry_price,
                    entered_at=rt.entered_at,
                    exit_price=rt.exit_price,
                    exited_at=rt.exited_at,
                    realized_pnl=rt.realized_pnl,
                    return_pct=rt.return_pct,
                )
                for rt in result.actual.round_trips
            ],
            open_positions=result.actual.open_positions,
            num_round_trips=result.actual.num_round_trips,
            num_winning=result.actual.num_winning,
            win_rate_pct=result.actual.win_rate_pct,
            total_realized_pnl=result.actual.total_realized_pnl,
            avg_return_pct=result.actual.avg_return_pct,
        ),
        expected=DeploymentExpectedPerformanceResponse(
            status=result.expected.status,
            reference_backtest_run_id=result.expected.reference_backtest_run_id,
            symbol=result.expected.symbol,
            total_return_pct=result.expected.total_return_pct,
            max_drawdown_pct=result.expected.max_drawdown_pct,
            win_rate_pct=result.expected.win_rate_pct,
            num_trades=result.expected.num_trades,
        ),
    )


def _drift_check_response(row: StrategyDriftCheck) -> DriftCheckResponse:
    return DriftCheckResponse(
        id=row.id,
        deployment_id=row.deployment_id,
        status=row.status,
        actual_win_rate_pct=row.actual_win_rate_pct,
        expected_win_rate_pct=row.expected_win_rate_pct,
        win_rate_deviation_pct=row.win_rate_deviation_pct,
        num_round_trips=row.num_round_trips,
        action_taken=row.action_taken,  # type: ignore[arg-type]
        detail=row.detail,
        created_at=row.created_at,
    )


@deployments_router.get(
    "/{deployment_id}/drift-checks", response_model=ListDriftChecksResponse
)
async def list_deployment_drift_checks(
    deployment_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListDriftChecksResponse:
    """Phase 66 (D084) - every drift check any of this deployment's
    SUCCEEDED runner cycles produced, newest first. Read-only: exactly as
    privileged as reading run history, same `strategy:deploy` permission and
    ownership check as `.../runs`/`.../signals`/`.../monitoring`, no new
    permission. A `status == "insufficient_data"` or `"no_drift"` row is not
    an error - it is the honest audit trail this feature keeps every cycle,
    matching the emergency-stop table's "a no-op flip still writes a row"
    philosophy."""
    await _load_owned_deployment(session, deployment_id, current_user)
    rows = (
        (
            await session.execute(
                select(StrategyDriftCheck)
                .where(StrategyDriftCheck.deployment_id == deployment_id)
                .order_by(
                    StrategyDriftCheck.created_at.desc(), StrategyDriftCheck.id.desc()
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListDriftChecksResponse(
        items=[_drift_check_response(r) for r in rows], limit=limit, offset=offset
    )
