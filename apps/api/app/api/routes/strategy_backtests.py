"""Persisted backtests of a strategy version - the Strategy Lab's second
surface (Phase 55).

**A NEW file, not an edit of routes/strategies.py.** That module is Phase
54's and stays as it is; the two ownership helpers it already got right
(`_load_owned_strategy`, `_load_version`) are IMPORTED from it below rather
than copied, so "not yours is 403, not there is 404, a version under
someone else's strategy is 404" is decided in exactly one place for both
surfaces. A second copy could drift, and an authorization check that drifts
is the worst kind.

**Two routers, one file.** The nested routes hang off `/strategies` because
a backtest is always OF a specific version and the path should say so; the
by-id read hangs off `/backtest-runs` because a run has its own identity
and a caller holding a run id should not have to also know which strategy
and version it came from to fetch it. Both carry the same router-level
`strategy:backtest` dependency.

**Two-part authorization, unchanged from D071.** The permission
(`strategy:backtest`, applied once at the router level exactly as
`strategy:manage` is applied to `/strategies`) says this account may run
backtests at all; the per-request ownership check says over which
strategies. Neither substitutes for the other, and holding the permission
authorizes acting on your own strategies and nobody else's - including on
`GET /backtest-runs/{id}`, which walks run -> version -> strategy -> owner
rather than trusting a run id to be unguessable.

**Only a VALIDATED version can be backtested** - 409 otherwise, naming the
validate endpoint. That is not a formality: `executor.generate_signals`'s
contract is that its definition already satisfies
`validate_definition(definition) == []`, and this check is what makes that
true. A draft's definition can be half-written by design (Phase 54's
editing rules depend on it), so running one would mean evaluating rules
nothing ever checked were expressible.

**No code execution anywhere in this path**, same hard boundary
routes/strategies.py and strategies/validation.py state: a definition is
walked as data against a closed vocabulary and dispatched to deterministic
functions. Nothing here compiles, evals, or imports by name.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.routes.strategies import _load_owned_strategy, _load_version
from apps.api.app.api.schemas_strategy_backtests import (
    BacktestEquityPointResponse,
    BacktestRunDetailResponse,
    BacktestRunSummary,
    BacktestTradeResponse,
    CreateBacktestRunRequest,
    ListBacktestRunsResponse,
)
from apps.api.app.auth.dependencies import get_current_user, require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.backtesting.engine_v2 import run_strategy_backtest
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    BacktestEquityPoint,
    BacktestRun,
    BacktestTrade,
    Strategy,
    StrategyVersion,
    StrategyVersionStatus,
    User,
)
from apps.api.app.marketdata.store import MarketDataStore
from apps.api.app.portfolio_manager.models import PortfolioLimits
from apps.api.app.risk.models import RiskLimits

router = APIRouter(
    prefix="/strategies",
    tags=["strategy-backtests"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_BACKTEST))],
)
"""The nested surface: run a backtest of one version, list that version's
runs. Same prefix as routes/strategies.py's router but a different
permission - a role may hold one of the two without the other."""

runs_router = APIRouter(
    prefix="/backtest-runs",
    tags=["strategy-backtests"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_BACKTEST))],
)
"""The by-id surface. A run is addressable on its own because the id is
what a POST response, a log line and a UI list row all carry."""

logger = get_logger(__name__)

DEFAULT_LIST_LIMIT = 50
"""The one pagination convention this codebase has - same default and same
ceiling as `GET /strategies` and every admin listing (D030)."""
MAX_LIST_LIMIT = 500


def _risk_limits(settings: Settings) -> RiskLimits:
    """Built from the same `Settings.risk_*` values the LIVE trade path
    reads, with no backtest-specific override - a backtest whose risk limits
    differed from production's would answer a question nobody asked (D035).
    Identical to what routes/backtests.py builds for D025's engine,
    including `require_stop_price=False`: a definition's exit rule is its
    exit, and demanding a stop price here would only force fabricating a
    value with no grounding in the strategy."""
    return RiskLimits(
        max_position_pct_of_equity=settings.risk_max_position_pct_of_equity,
        max_portfolio_exposure_pct_of_equity=settings.risk_max_portfolio_exposure_pct_of_equity,
        max_risk_pct_of_equity_per_trade=settings.risk_max_risk_pct_of_equity_per_trade,
        require_stop_price=False,
        max_market_data_age_seconds=settings.risk_max_market_data_age_seconds,
        duplicate_order_window_seconds=settings.risk_duplicate_order_window_seconds,
    )


def _portfolio_limits(settings: Settings) -> PortfolioLimits:
    """Same `Settings.portfolio_*` values the live trade path uses, for the
    same reason as `_risk_limits` above (D035)."""
    return PortfolioLimits(
        max_symbol_pct_of_equity=settings.portfolio_max_symbol_pct_of_equity,
        min_cash_reserve_pct_of_equity=settings.portfolio_min_cash_reserve_pct_of_equity,
        max_open_positions=settings.portfolio_max_open_positions,
    )


def _run_summary(run: BacktestRun) -> BacktestRunSummary:
    return BacktestRunSummary(
        id=run.id,
        strategy_version_id=run.strategy_version_id,
        symbol=run.symbol,
        bar_interval=run.bar_interval,
        start_date=run.start_date,
        end_date=run.end_date,
        starting_cash=run.starting_cash,
        status=run.status,
        final_equity=run.final_equity,
        total_return_pct=run.total_return_pct,
        max_drawdown_pct=run.max_drawdown_pct,
        win_rate_pct=run.win_rate_pct,
        num_trades=run.num_trades,
        error_detail=run.error_detail,
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


async def _run_detail(session: AsyncSession, run: BacktestRun) -> BacktestRunDetailResponse:
    """The summary plus this run's curve and trades, each in one query.

    The curve is ordered by `date` and the trades by `entry_date` so a
    client never has to sort them itself; a FAILED run simply has neither,
    which is the honest empty result rather than a fabricated flat line.
    """
    equity_rows = (
        (
            await session.execute(
                select(BacktestEquityPoint)
                .where(BacktestEquityPoint.backtest_run_id == run.id)
                .order_by(BacktestEquityPoint.date.asc())
            )
        )
        .scalars()
        .all()
    )
    trade_rows = (
        (
            await session.execute(
                select(BacktestTrade)
                .where(BacktestTrade.backtest_run_id == run.id)
                .order_by(BacktestTrade.entry_date.asc())
            )
        )
        .scalars()
        .all()
    )
    return BacktestRunDetailResponse(
        **_run_summary(run).model_dump(),
        equity_curve=[
            BacktestEquityPointResponse(date=row.date, equity=row.equity) for row in equity_rows
        ],
        trades=[
            BacktestTradeResponse(
                side=row.side,
                entry_date=row.entry_date,
                entry_price=row.entry_price,
                exit_date=row.exit_date,
                exit_price=row.exit_price,
                quantity=row.quantity,
                return_pct=row.return_pct,
            )
            for row in trade_rows
        ],
    )


@router.post(
    "/{strategy_id}/versions/{version_id}/backtests",
    response_model=BacktestRunDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_backtest_run(
    strategy_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: CreateBacktestRunRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> BacktestRunDetailResponse:
    """Runs the version over the requested symbol and window, persists the
    result, and returns it IN FULL - equity curve and trades included,
    unlike the listing route. The caller who just triggered a run is the one
    person guaranteed to want to look at it, so making them issue a second
    request for the numbers they asked for would be pure ceremony.

    409 when the version is not `validated`, naming the validate endpoint.
    See the module docstring: this is what makes the signal evaluator's
    already-validated precondition true.

    Runs SYNCHRONOUSLY, to completion, inside this request - the same
    posture `run_backfill_job` (D070) and D025's backtest engine both take.
    There is no queue and no background worker, which is exactly why the run
    row can be created and finished within one transaction with nothing able
    to race it.

    A 201 does NOT mean the strategy made money, or even that the run
    succeeded: a failed run is a real, persisted resource and comes back
    with `status: "failed"` and a real `error_detail` (most often a bar-data
    gap). That is deliberate - `POST /backtests`, D025's unpersisted
    endpoint, answers 400 on the same condition because it has nothing to
    show for the attempt. This one does.
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

    run = await run_strategy_backtest(
        session=session,
        strategy_version=version,
        symbol=payload.symbol,
        bar_interval=payload.bar_interval,
        start_date=payload.start_date,
        end_date=payload.end_date,
        starting_cash=payload.starting_cash,
        # The persisted bar store (D070) is the data source - never a live
        # vendor call. A backtest reads only bars that were really ingested,
        # so the same window run twice reads the same bars twice.
        bar_provider=MarketDataStore(session),
        risk_limits=_risk_limits(settings),
        portfolio_limits=_portfolio_limits(settings),
        requested_by_user_id=current_user.id,
    )
    # The engine already committed - both on success and on failure - so
    # there is deliberately no second commit here.

    logger.info(
        "backtest_run_requested",
        backtest_run_id=str(run.id),
        strategy_id=str(strategy_id),
        strategy_version_id=str(version_id),
        symbol=payload.symbol,
        status=run.status.value,
        actor_user_id=str(current_user.id),
    )
    return await _run_detail(session, run)


@router.get(
    "/{strategy_id}/versions/{version_id}/backtests",
    response_model=ListBacktestRunsResponse,
)
async def list_backtest_runs(
    strategy_id: uuid.UUID,
    version_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListBacktestRunsResponse:
    """This version's runs, NEWEST `created_at` first, as summaries.

    Newest-first rather than the oldest-first ordering the /strategies and
    /admin listings use, and for the same reason `StrategyDetailResponse`
    lists versions newest-first: the run someone wants is almost always the
    one they just did. `id` is the tiebreaker that keeps `offset` paging
    deterministic when two runs share a timestamp.

    Summaries only - no equity curve, no trades. Fifty runs of a
    two-hundred-bar window would otherwise be ten thousand curve points to
    render a table. Full detail is one request away per run
    (`GET /backtest-runs/{id}`), matching the lean-list/full-detail split
    `StrategyVersionSummary` established.
    """
    await _load_owned_strategy(session, strategy_id, current_user)
    await _load_version(session, strategy_id, version_id)

    rows = (
        (
            await session.execute(
                select(BacktestRun)
                .where(BacktestRun.strategy_version_id == version_id)
                .order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListBacktestRunsResponse(
        items=[_run_summary(row) for row in rows], limit=limit, offset=offset
    )


@runs_router.get("/{run_id}", response_model=BacktestRunDetailResponse)
async def get_backtest_run(
    run_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BacktestRunDetailResponse:
    """One run in full, addressed by its own id.

    Ownership is re-derived here, never assumed from possession of the id:
    the run's `strategy_version_id` leads to a version, which leads to a
    strategy, which carries `owner_user_id`. One join does all three, so
    there is no ordering of events in which another user's run is fetched
    and then filtered out.

    404 when no run has this id, 403 when one does but the caller does not
    own the strategy behind it - the same order and the same two codes
    `_load_owned_strategy` uses, for the same reason: a nonexistent id must
    not 403 merely because nobody can own a row that isn't there.
    """
    row = (
        await session.execute(
            select(BacktestRun, Strategy.owner_user_id)
            .join(StrategyVersion, StrategyVersion.id == BacktestRun.strategy_version_id)
            .join(Strategy, Strategy.id == StrategyVersion.strategy_id)
            .where(BacktestRun.id == run_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No backtest run with id {run_id}.")

    run, owner_user_id = row
    if owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail=f"Backtest run {run_id} is not yours.")

    return await _run_detail(session, run)
