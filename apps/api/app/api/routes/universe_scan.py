"""Universe scans over a strategy version - the Strategy Lab's newest
surface (Phase 60): run one validated definition across many symbols and
rank them.

**A NEW file, not an edit of routes/strategy_backtests.py or
routes/walk_forward.py.** Those modules are Phases 55 and 57's and stay as
they are. What they already got right is imported rather than copied: the
two ownership helpers (`_load_owned_strategy`, `_load_version`) and the
`_risk_limits` / `_portfolio_limits` builders. The limit builders matter as
much as the authorization helpers do - every symbol here runs through
`run_strategy_backtest`, so limits built differently in this module would
mean a scanned symbol silently ran under different risk rules than the plain
backtest a caller compares it against. One definition, no drift.

**Two routers, one file**, the same split routes/strategy_backtests.py and
routes/walk_forward.py already use: the nested routes hang off `/strategies`
because a scan is always OF a specific version and the path should say so;
the by-id read hangs off `/universe-scans` because a scan has its own
identity and a caller holding an id should not need to know which strategy
and version it came from. Both carry the same router-level
`strategy:backtest` dependency - deliberately the SAME permission Phase 55
added rather than a new one, because a scan is n backtests and nothing more:
anyone who may run one may run several.

**Two-part authorization, unchanged from D071.** The permission says this
account may run backtests at all; the per-request ownership check says over
which strategies. `GET /universe-scans/{id}` re-derives ownership by walking
scan -> version -> strategy -> owner rather than trusting an id to be
unguessable.

**Only a VALIDATED version can be scanned** - 409 otherwise, same message
pattern and same reason as the backtest and walk-forward routes: it is what
makes the signal evaluator's already-validated precondition true.

**The universe is resolved BEFORE any row is created.** An empty or
over-cap universe is a malformed request, so it is a 422 that persists
nothing - not a FAILED scan row. That is the opposite call from
walk-forward's "too few windows", and deliberately so: how many windows fit
a range is an outcome of the boundary rules the orchestrator owns and can
only be known by applying them, whereas "you sent no symbols" or "you sent
51" is knowable from the request itself, and answering it with a persisted
failure would file a result row for something that never ran.
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
    _risk_limits,
)
from apps.api.app.api.schemas_universe_scan import (
    CreateUniverseScanRequest,
    ListUniverseScansResponse,
    UniverseScanDetailResponse,
    UniverseScanResultResponse,
    UniverseScanSummary,
)
from apps.api.app.auth.dependencies import get_current_user, require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.backtesting.universe_scan import (
    MAX_SCAN_SYMBOLS,
    order_and_rank_results,
    resolve_scan_symbols,
    run_universe_scan,
)
from apps.api.app.core.config import Settings, get_settings
from apps.api.app.core.logging import get_logger
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    Strategy,
    StrategyVersion,
    StrategyVersionStatus,
    UniverseScan,
    UniverseScanResult,
    User,
)
from apps.api.app.marketdata.store import MarketDataStore

router = APIRouter(
    prefix="/strategies",
    tags=["universe-scan"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_BACKTEST))],
)
"""The nested surface: scan a universe with one version, list that version's
scans."""

scans_router = APIRouter(
    prefix="/universe-scans",
    tags=["universe-scan"],
    dependencies=[Depends(require_permission(Permission.STRATEGY_BACKTEST))],
)
"""The by-id surface. A scan is addressable on its own because the id is what
a POST response, a log line and a UI list row all carry."""

logger = get_logger(__name__)


def _scan_summary(scan: UniverseScan) -> UniverseScanSummary:
    return UniverseScanSummary(
        id=scan.id,
        strategy_version_id=scan.strategy_version_id,
        bar_interval=scan.bar_interval,
        start_date=scan.start_date,
        end_date=scan.end_date,
        starting_cash=scan.starting_cash,
        scan_mode=scan.scan_mode,
        requested_symbols=scan.requested_symbols,
        status=scan.status,
        num_symbols=scan.num_symbols,
        num_succeeded=scan.num_succeeded,
        num_qualified=scan.num_qualified,
        error_detail=scan.error_detail,
        created_at=scan.created_at,
        completed_at=scan.completed_at,
    )


async def _scan_detail(session: AsyncSession, scan: UniverseScan) -> UniverseScanDetailResponse:
    """The summary plus this scan's per-symbol results, ALREADY RANKED, so a
    client never has to sort them itself and two clients cannot disagree
    about the order.

    Ranking is derived here on read by `order_and_rank_results` - the one
    definition of what "best" means, shared with the orchestrator's own tests
    - rather than read from a stored column that a later change to the rule
    would leave stale.

    A FAILED scan simply has no results - or, when every symbol's own
    backtest failed, has all of them with their real failed `backtest_run_id`s
    and `rank: null`. Both are the honest answer rather than a fabricated
    ranking.
    """
    rows = (
        (
            await session.execute(
                select(UniverseScanResult).where(UniverseScanResult.universe_scan_id == scan.id)
            )
        )
        .scalars()
        .all()
    )
    return UniverseScanDetailResponse(
        **_scan_summary(scan).model_dump(),
        results=[
            UniverseScanResultResponse(
                id=row.id,
                symbol=row.symbol,
                backtest_run_id=row.backtest_run_id,
                status=row.status,
                total_return_pct=row.total_return_pct,
                max_drawdown_pct=row.max_drawdown_pct,
                win_rate_pct=row.win_rate_pct,
                num_trades=row.num_trades,
                error_detail=row.error_detail,
                rank=rank,
            )
            for row, rank in order_and_rank_results(rows)
        ],
    )


@router.post(
    "/{strategy_id}/versions/{version_id}/universe-scans",
    response_model=UniverseScanDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_universe_scan(
    strategy_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: CreateUniverseScanRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> UniverseScanDetailResponse:
    """Runs a real backtest of this version over every symbol in the
    universe, then returns the scan WITH its ranked per-symbol breakdown -
    the caller who just triggered it is the one person guaranteed to want to
    see which symbols ranked where.

    Runs SYNCHRONOUSLY, to completion, inside this request - n backtests
    where `POST .../backtests` runs one, the same posture and for the same
    reason as walk-forward (no queue, no worker, nothing able to race the
    row). `MAX_SCAN_SYMBOLS` is what keeps that honest; a bigger universe
    needs a job runner this codebase does not have yet.

    A 201 does NOT mean any symbol is worth trading, or even that any
    symbol's backtest succeeded: a scan where every symbol lacked history is
    a real, persisted `succeeded` scan whose results all say so, and a scan
    that failed outright comes back with `status: "failed"` and a real
    `error_detail`. Same deliberate choice the persisted backtest and
    walk-forward routes made.
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

    if payload.symbols is not None and not payload.symbols:
        raise HTTPException(
            status_code=422,
            detail=(
                "EMPTY_SYMBOL_LIST: pass symbols to scan, or omit the field entirely "
                "to scan all ingested symbols."
            ),
        )

    # The same resolver the orchestrator will use, so the count checked here
    # and the universe actually scanned can never be two different things.
    resolved = await resolve_scan_symbols(session, payload.symbols, payload.bar_interval)

    if payload.symbols is not None:
        # An explicit list can also normalize down to nothing (e.g. [" "]),
        # which is the same request problem as [] and gets the same answer.
        if not resolved:
            raise HTTPException(
                status_code=422,
                detail=(
                    "EMPTY_SYMBOL_LIST: pass symbols to scan, or omit the field entirely "
                    "to scan all ingested symbols."
                ),
            )
        if len(resolved) > MAX_SCAN_SYMBOLS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"TOO_MANY_SYMBOLS: {len(resolved)} symbols requested, over the "
                    f"{MAX_SCAN_SYMBOLS} cap for a synchronous scan. Split the universe "
                    "across several scans."
                ),
            )
    elif not resolved:
        raise HTTPException(
            status_code=422,
            detail=(
                f"NO_SYMBOLS_INGESTED: no symbols are ingested for interval "
                f"'{payload.bar_interval}'; backfill some via "
                "POST /admin/market-data/backfill first."
            ),
        )
    elif len(resolved) > MAX_SCAN_SYMBOLS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"TOO_MANY_SYMBOLS: {len(resolved)} symbols are ingested, over the "
                f"{MAX_SCAN_SYMBOLS} cap for a synchronous scan; pass an explicit "
                "`symbols` list to narrow it."
            ),
        )

    scan = await run_universe_scan(
        session=session,
        strategy_version=version,
        symbols=payload.symbols,
        bar_interval=payload.bar_interval,
        start_date=payload.start_date,
        end_date=payload.end_date,
        starting_cash=payload.starting_cash,
        # The persisted bar store (D070) is the data source - never a live
        # vendor call, exactly as the single-backtest route requires.
        bar_provider=MarketDataStore(session),
        risk_limits=_risk_limits(settings),
        portfolio_limits=_portfolio_limits(settings),
        cost_model=_cost_model(settings),
        requested_by_user_id=current_user.id,
    )
    # The orchestrator already committed - both on success and on failure -
    # so there is deliberately no second commit here.

    logger.info(
        "universe_scan_requested",
        universe_scan_id=str(scan.id),
        strategy_id=str(strategy_id),
        strategy_version_id=str(version_id),
        scan_mode=scan.scan_mode,
        num_symbols=len(resolved),
        status=scan.status.value,
        actor_user_id=str(current_user.id),
    )
    return await _scan_detail(session, scan)


@router.get(
    "/{strategy_id}/versions/{version_id}/universe-scans",
    response_model=ListUniverseScansResponse,
)
async def list_universe_scans(
    strategy_id: uuid.UUID,
    version_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListUniverseScansResponse:
    """This version's universe scans, NEWEST `created_at` first, as
    summaries.

    Newest-first and summaries-only for the reasons the backtest and
    walk-forward listings already give: the scan someone wants is almost
    always the one they just did, and a table of counts should not drag
    every scan's fifty-symbol breakdown along with it. `id` is the tiebreaker
    that keeps `offset` paging deterministic when two scans share a
    timestamp.
    """
    await _load_owned_strategy(session, strategy_id, current_user)
    await _load_version(session, strategy_id, version_id)

    rows = (
        (
            await session.execute(
                select(UniverseScan)
                .where(UniverseScan.strategy_version_id == version_id)
                .order_by(UniverseScan.created_at.desc(), UniverseScan.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListUniverseScansResponse(
        items=[_scan_summary(row) for row in rows], limit=limit, offset=offset
    )


@scans_router.get("/{scan_id}", response_model=UniverseScanDetailResponse)
async def get_universe_scan(
    scan_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> UniverseScanDetailResponse:
    """One universe scan with its ranked results, addressed by its own id.

    Ownership is re-derived here, never assumed from possession of the id:
    the scan's `strategy_version_id` leads to a version, which leads to a
    strategy, which carries `owner_user_id`. One join does all three, so
    there is no ordering of events in which another user's scan is fetched
    and then filtered out.

    404 when no scan has this id, 403 when one does but the caller does not
    own the strategy behind it - the same order and the same two codes
    `_load_owned_strategy` and `GET /walk-forward-runs/{id}` use, for the
    same reason: a nonexistent id must not 403 merely because nobody can own
    a row that isn't there.
    """
    row = (
        await session.execute(
            select(UniverseScan, Strategy.owner_user_id)
            .join(StrategyVersion, StrategyVersion.id == UniverseScan.strategy_version_id)
            .join(Strategy, Strategy.id == StrategyVersion.strategy_id)
            .where(UniverseScan.id == scan_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No universe scan with id {scan_id}.")

    scan, owner_user_id = row
    if owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail=f"Universe scan {scan_id} is not yours.")

    return await _scan_detail(session, scan)
