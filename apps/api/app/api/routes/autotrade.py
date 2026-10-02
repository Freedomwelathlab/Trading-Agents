"""Autotrade Bot routes (Phase 81, D098).

    POST   /autotrade/bots                 create (PENDING_APPROVAL)      strategy:deploy
    GET    /autotrade/bots                 the caller's bots
    GET    /autotrade/bots/{id}
    POST   /autotrade/bots/{id}/approve    -> ACTIVE          strategy:approve_deployment
    POST   /autotrade/bots/{id}/pause
    POST   /autotrade/bots/{id}/resume
    POST   /autotrade/bots/{id}/stop
    POST   /autotrade/bots/{id}/run        one cycle now (any status; a
                                           non-active bot writes its
                                           SKIPPED_NOT_ACTIVE row)
    GET    /autotrade/bots/{id}/runs
    GET    /autotrade/bots/{id}/trades
    GET    /autotrade/bots/{id}/stats      per-setup learning-loop numbers
    GET    /autotrade/setups               the registered setup names

Permissions reuse the deployment ones deliberately: creating a robot that
will place paper orders is the same class of act as deploying a strategy
(`STRATEGY_DEPLOY`), and approving one is the same human gate
(`STRATEGY_APPROVE_DEPLOYMENT`). Ownership is enforced on every read and
action: a bot is visible to, and controllable by, the account that
created it (or `admin:manage`).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.schemas_scan import BotScanResponse, SymbolBoardResponse
from apps.api.app.auth.dependencies import get_current_user, require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.autotrade.learning import (
    active_setups,
    closed_trades,
    stats_by_setup,
    stats_by_setup_hour,
    stats_by_setup_symbol,
)
from apps.api.app.autotrade.runner import run_autotrade_cycle
from apps.api.app.autotrade.service import (
    AutotradeError,
    BotSpec,
    approve_bot,
    create_bot,
    pause_bot,
    resume_bot,
    stop_bot,
)
from apps.api.app.backtesting.setups import SETUPS
from apps.api.app.core.config import get_settings
from apps.api.app.db.base import get_session, get_session_factory
from apps.api.app.db.models import (
    AutotradeBot,
    AutotradeBotInsight,
    AutotradeBotRun,
    AutotradeBotStatus,
    AutotradeBotTrade,
    User,
)
from apps.api.app.marketdata.sessions import session_phase

router = APIRouter(prefix="/autotrade", tags=["autotrade"])

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 500


# --- schemas ---------------------------------------------------------------


class CreateBotRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    broker_id: uuid.UUID
    watchlist_id: uuid.UUID | None = None
    symbols: list[str] = Field(default_factory=list)
    """Explicit symbols. Empty with a `watchlist_id` means "every symbol on
    that watchlist right now"."""
    market_type: str = "regular"
    bar_interval: str = "5m"
    max_trades_per_session: int = Field(ge=1, le=50)
    max_trades_per_day: int = Field(ge=1, le=200)
    capital_per_trade: Decimal = Field(gt=0)
    strategy_mode: str = "auto"
    setups: list[str] = Field(default_factory=list)
    min_score: int = Field(default=3, ge=0, le=10)
    extended_hours_min_score: int | None = Field(default=None, ge=0, le=10)
    """Phase 87 (D106): the score a signal must reach on a PRE-MARKET or
    AFTER-HOURS bar. Left unset on a bot whose `market_type` admits those
    phases, the route fills in a stricter figure than `min_score` rather
    than treating a 04:30 print as evidence equal to a 10:30 one."""
    allow_short: bool = False
    """Both directions. Off by default: the setups have always emitted
    short signals and the bot has always discarded them, so turning this
    on is a genuine change in what the bot may do."""
    stop_loss_mode: str = "auto"
    stop_loss_max_pct: Decimal | None = None
    trailing_stop_pct: Decimal | None = None
    take_profit_mode: str = "auto"
    take_profit_min_pct: Decimal | None = None
    trailing_take_profit_pct: Decimal | None = None
    news_blackout_minutes: int = Field(default=0, ge=0, le=1440)


class PauseBotRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class BotResponse(BaseModel):
    id: uuid.UUID
    name: str
    broker_id: uuid.UUID
    status: str
    watchlist_id: uuid.UUID | None
    symbols: list[str]
    market_type: str
    bar_interval: str
    max_trades_per_session: int
    max_trades_per_day: int
    capital_per_trade: Decimal
    strategy_mode: str
    setups: list[str]
    min_score: int
    extended_hours_min_score: int | None
    allow_short: bool
    stop_loss_mode: str
    stop_loss_max_pct: Decimal | None
    trailing_stop_pct: Decimal | None
    take_profit_mode: str
    take_profit_min_pct: Decimal | None
    trailing_take_profit_pct: Decimal | None
    news_blackout_minutes: int
    approved_at: datetime | None
    paused_reason: str | None
    stopped_at: datetime | None
    last_evaluated_at: datetime | None
    created_at: datetime
    asset_class: str = "equity"
    """Phase 107 (D133): crypto / forex / equity, from the bot's symbols."""

    @classmethod
    def from_row(cls, b: AutotradeBot) -> BotResponse:
        from apps.api.app.bots.scanboard import asset_class_of

        classes = {asset_class_of(s) for s in b.symbols}
        return cls(
            asset_class=classes.pop() if len(classes) == 1 else "mixed",
            id=b.id,
            name=b.name,
            broker_id=b.broker_id,
            status=b.status.value,
            watchlist_id=b.watchlist_id,
            symbols=list(b.symbols),
            market_type=b.market_type,
            bar_interval=b.bar_interval,
            max_trades_per_session=b.max_trades_per_session,
            max_trades_per_day=b.max_trades_per_day,
            capital_per_trade=b.capital_per_trade,
            strategy_mode=b.strategy_mode,
            setups=list(b.setups),
            min_score=b.min_score,
            extended_hours_min_score=b.extended_hours_min_score,
            allow_short=b.allow_short,
            stop_loss_mode=b.stop_loss_mode,
            stop_loss_max_pct=b.stop_loss_max_pct,
            trailing_stop_pct=b.trailing_stop_pct,
            take_profit_mode=b.take_profit_mode,
            take_profit_min_pct=b.take_profit_min_pct,
            trailing_take_profit_pct=b.trailing_take_profit_pct,
            news_blackout_minutes=b.news_blackout_minutes,
            approved_at=b.approved_at,
            paused_reason=b.paused_reason,
            stopped_at=b.stopped_at,
            last_evaluated_at=b.last_evaluated_at,
            created_at=b.created_at,
        )


class ListBotsResponse(BaseModel):
    bots: list[BotResponse]


class BotRunResponse(BaseModel):
    id: uuid.UUID
    status: str
    started_at: datetime
    completed_at: datetime | None
    symbols_scanned: int
    signals_found: int
    signals_skipped: int
    trades_opened: int
    trades_closed: int
    setups_active: list[str]
    detail: str | None


class ListBotRunsResponse(BaseModel):
    runs: list[BotRunResponse]
    limit: int
    offset: int


class BotTradeResponse(BaseModel):
    id: uuid.UUID
    symbol: str
    session_date: date
    setup_name: str
    score: int
    evidence: dict[str, Any]
    quantity: Decimal
    entry_price: Decimal
    initial_stop_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal | None
    peak_price: Decimal
    take_profit_armed: bool
    opened_at: datetime
    closed_at: datetime | None
    exit_price: Decimal | None
    exit_reason: str | None
    realized_pnl: Decimal | None
    r_multiple: Decimal | None
    entry_order_id: uuid.UUID | None
    exit_order_id: uuid.UUID | None


class ListBotTradesResponse(BaseModel):
    trades: list[BotTradeResponse]
    limit: int
    offset: int


class SetupStatsResponse(BaseModel):
    setup_name: str
    trades: int
    wins: int
    win_rate: Decimal | None
    expectancy_r: Decimal | None
    total_r: Decimal
    total_pnl: Decimal
    demoted: bool
    symbol: str | None = None
    hour: int | None = None
    avg_mfe_r: Decimal | None = None
    avg_mae_r: Decimal | None = None


class BotStatsResponse(BaseModel):
    bot_id: uuid.UUID
    strategy_mode: str
    configured_setups: list[str]
    active_setups: list[str]
    setups: list[SetupStatsResponse]
    by_symbol: list[SetupStatsResponse]
    by_hour: list[SetupStatsResponse]
    closed_trades: int
    total_r: Decimal
    total_pnl: Decimal


class BotInsightResponse(BaseModel):
    id: uuid.UUID
    session_date: date
    trades: int
    wins: int
    total_r: Decimal
    total_pnl: Decimal
    best_setup: str | None
    worst_setup: str | None
    findings: list[str]
    demoted_setups: list[str]
    created_at: datetime


class ListBotInsightsResponse(BaseModel):
    insights: list[BotInsightResponse]


class RunNowResponse(BaseModel):
    status: str
    detail: str | None
    run_status: str | None
    run_detail: str | None
    symbols_scanned: int
    signals_found: int
    trades_opened: int
    trades_closed: int


class DeleteBotsRequest(BaseModel):
    bot_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class DeleteBotResult(BaseModel):
    bot_id: uuid.UUID
    name: str | None
    outcome: str
    """`deleted`, `refused` or `not_found`."""
    reason: str | None = None


class DeleteBotsResponse(BaseModel):
    results: list[DeleteBotResult]


class SetupsResponse(BaseModel):
    setups: list[str]


# --- helpers ---------------------------------------------------------------


async def _load_owned_bot(session: AsyncSession, bot_id: uuid.UUID, user: User) -> AutotradeBot:
    bot = await session.get(AutotradeBot, bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"No bot with id {bot_id}.")
    is_admin = user.role is not None and Permission.ADMIN.value in (user.role.permissions or [])
    if bot.owner_user_id != user.id and not is_admin:
        raise HTTPException(status_code=403, detail="This bot belongs to another account.")
    return bot


def _autotrade_error(exc: AutotradeError) -> HTTPException:
    code = str(exc).split(":", 1)[0]
    status_code = {
        "NO_SUCH_BROKER": 404,
        "NO_SUCH_WATCHLIST": 404,
        "NO_BROKER_GRANT": 403,
        "NOT_PENDING_APPROVAL": 409,
        "NOT_ACTIVE": 409,
        "NOT_PAUSED": 409,
        "ALREADY_STOPPED": 409,
    }.get(code, 400)
    return HTTPException(status_code=status_code, detail=str(exc))


# --- routes ----------------------------------------------------------------


@router.get("/setups", response_model=SetupsResponse)
async def list_setups(_: User = Depends(get_current_user)) -> SetupsResponse:
    return SetupsResponse(setups=sorted(SETUPS))


@router.post("/bots", response_model=BotResponse, status_code=status.HTTP_201_CREATED)
async def create_autotrade_bot(
    payload: CreateBotRequest,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
) -> BotResponse:
    spec = BotSpec(
        name=payload.name,
        broker_id=payload.broker_id,
        watchlist_id=payload.watchlist_id,
        symbols=payload.symbols,
        market_type=payload.market_type,
        bar_interval=payload.bar_interval,
        max_trades_per_session=payload.max_trades_per_session,
        max_trades_per_day=payload.max_trades_per_day,
        capital_per_trade=payload.capital_per_trade,
        strategy_mode=payload.strategy_mode,
        setups=payload.setups,
        min_score=payload.min_score,
        extended_hours_min_score=payload.extended_hours_min_score,
        allow_short=payload.allow_short,
        stop_loss_mode=payload.stop_loss_mode,
        stop_loss_max_pct=payload.stop_loss_max_pct,
        trailing_stop_pct=payload.trailing_stop_pct,
        take_profit_mode=payload.take_profit_mode,
        take_profit_min_pct=payload.take_profit_min_pct,
        trailing_take_profit_pct=payload.trailing_take_profit_pct,
        news_blackout_minutes=payload.news_blackout_minutes,
    )
    try:
        bot = await create_bot(session, spec, user_id=current_user.id)
    except AutotradeError as exc:
        raise _autotrade_error(exc) from None
    await session.commit()
    await session.refresh(bot)
    return BotResponse.from_row(bot)


@router.get("/bots", response_model=ListBotsResponse)
async def list_autotrade_bots(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListBotsResponse:
    rows = (
        (
            await session.execute(
                select(AutotradeBot)
                .where(AutotradeBot.owner_user_id == current_user.id)
                .order_by(AutotradeBot.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return ListBotsResponse(bots=[BotResponse.from_row(b) for b in rows])


@router.post("/bots/delete", response_model=DeleteBotsResponse)
async def delete_autotrade_bots(
    payload: DeleteBotsRequest,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
) -> DeleteBotsResponse:
    """Phase 106 (D131): delete unused bots, answered per bot.

    * **refused** while the bot is ACTIVE (pause or stop it first), or while
      it still holds an open position - stopping never liquidates, so
      deleting such a bot would orphan a position nothing then manages.
    * **deleted** otherwise, with its runs, trade ledger and insights. The
      orders it placed are ordinary `orders` rows and stay in the order
      history, which is the audit trail.
    * **not_found** for an id that is not one of this user's bots (an admin
      may delete any bot, as with every other bot action).
    """
    is_admin = current_user.role is not None and Permission.ADMIN.value in (
        current_user.role.permissions or []
    )
    results: list[DeleteBotResult] = []
    for bot_id in dict.fromkeys(payload.bot_ids):  # de-duplicated, order kept
        bot = await session.get(AutotradeBot, bot_id)
        if bot is None or (bot.owner_user_id != current_user.id and not is_admin):
            results.append(DeleteBotResult(bot_id=bot_id, name=None, outcome="not_found"))
            continue
        if bot.status is AutotradeBotStatus.ACTIVE:
            results.append(
                DeleteBotResult(
                    bot_id=bot_id,
                    name=bot.name,
                    outcome="refused",
                    reason="The bot is active - pause or stop it first.",
                )
            )
            continue
        open_trades = (
            (
                await session.execute(
                    select(AutotradeBotTrade.symbol).where(
                        AutotradeBotTrade.bot_id == bot.id, AutotradeBotTrade.closed_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        if open_trades:
            results.append(
                DeleteBotResult(
                    bot_id=bot_id,
                    name=bot.name,
                    outcome="refused",
                    reason=(
                        f"The bot still holds {', '.join(sorted(set(open_trades)))} - close the "
                        "position on the desk first; stopping a bot never sells."
                    ),
                )
            )
            continue
        await session.delete(bot)
        results.append(DeleteBotResult(bot_id=bot_id, name=bot.name, outcome="deleted"))
    await session.commit()
    return DeleteBotsResponse(results=results)


ASSET_EVIDENCE: dict[str, list[str]] = {
    "crypto": [
        "No crypto walk-forward has been run on this platform yet. Confidence figures are "
        "each coin's own last 60 sessions on its 24-hour clock (+1R before the stop).",
        "Crypto trades around the clock; a 24-hour bot is never forced flat.",
    ],
    "forex": [
        "EUR/USD walk-forward (D123): every setup lost after a 1-pip round trip; pooled "
        "-0.078R per trade, t -1.96 over 764 trades. sweep_mss had a real gross edge "
        "(+0.161R, t 4.7) that breaks even near 1.1 pips.",
        "Bars are mid prices with no volume, so volume-based setups never fire on FX.",
    ],
    "equity": [
        "TQQQ 5m walk-forward with the stop buffer searched (D125): 1 promising setup "
        "(bollinger_confluence +0.058R, t 0.18, 23 trades), 0 recommended.",
    ],
}


@router.get("/bots/{bot_id}/scan", response_model=BotScanResponse)
async def scan_autotrade_bot(
    bot_id: uuid.UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BotScanResponse:
    """Phase 107 (D133): the bot's dashboard - every setup scored on each
    symbol's latest bar, the recommendation it adds up to, and the
    research evidence for the asset class. Read-only: places nothing. For
    crypto and forex the newest bars are fetched first (same throttled path
    as the charts); equities read what the bot's last cycle stored."""
    from datetime import UTC, timedelta

    from apps.api.app.api.routes.marketdata import refresh_live_bars
    from apps.api.app.autotrade.engine import bot_calendar
    from apps.api.app.autotrade.profiles import deployable_profiles
    from apps.api.app.backtesting.brackets import BracketPlan
    from apps.api.app.bots.scanboard import (
        asset_class_of,
        build_board,
        calibration_or_start,
    )
    from apps.api.app.db.base import get_session_factory
    from apps.api.app.marketdata.sessions import SessionPhase
    from apps.api.app.marketdata.store import MarketDataStore
    from apps.api.app.marketdata.structure import Direction

    bot = await _load_owned_bot(session, bot_id, current_user)
    history = await closed_trades(session, bot.id)
    enabled = active_setups(
        list(bot.setups), strategy_mode=bot.strategy_mode, stats=stats_by_setup(history)
    )
    directions = (Direction.LONG, Direction.SHORT) if bot.allow_short else (Direction.LONG,)
    backfill = request.app.state.market_data_bar_backfill_provider
    store = MarketDataStore(session)
    today = datetime.now(UTC).date()
    history_days = 12 if bot.bar_interval == "1m" else 92
    boards = []
    for symbol in bot.symbols:
        cls = asset_class_of(symbol)
        if cls == "crypto" or (cls == "forex" and "fx" in backfill.configured_roles):
            await refresh_live_bars(
                session, backfill, symbol, bot.bar_interval, today - timedelta(days=3), today
            )
        bars = await store.get_bars(
            symbol, bar_interval=bot.bar_interval,
            start_date=today - timedelta(days=6), end_date=today,
        )

        def loader(symbol: str = symbol, cls: str = cls):
            async def load() -> list:
                # Crypto history comes straight from the free vendor; equity
                # and FX from what is stored (FX is metered - D132).
                start = today - timedelta(days=history_days)
                if cls == "crypto":
                    return await backfill.provider_for(symbol).get_bars(
                        symbol, bar_interval=bot.bar_interval, start_date=start, end_date=today
                    )
                async with get_session_factory()() as own:
                    return await MarketDataStore(own).get_bars(
                        symbol, bar_interval=bot.bar_interval, start_date=start, end_date=today
                    )

            return load

        symbol_enabled = enabled
        if bot.strategy_mode == "tuned":
            symbol_enabled = [p.setup for p in deployable_profiles(symbol, bot.bar_interval)]
        min_score = bot.min_score
        if bot.extended_hours_min_score is not None and bars and bot_calendar(
            symbol, bot.market_type
        ) is None and session_phase(bars[-1].ts) in (
            SessionPhase.PRE_MARKET, SessionPhase.AFTER_HOURS
        ):
            min_score = max(min_score, bot.extended_hours_min_score)
        calibration, calibration_status = calibration_or_start(
            symbol, bot.bar_interval, bot.market_type, loader()
        )
        board = build_board(
            symbol, bars, enabled_setups=symbol_enabled, market_type=bot.market_type,
            plan=BracketPlan(), min_score=min_score, allow_directions=directions,
            calibration=calibration,
        )
        boards.append(SymbolBoardResponse.of(board, calibration=calibration_status))
    classes = {asset_class_of(s) for s in bot.symbols}
    asset_class = classes.pop() if len(classes) == 1 else "mixed"
    return BotScanResponse(
        bot_id=str(bot.id), name=bot.name, asset_class=asset_class,
        market_type=bot.market_type, bar_interval=bot.bar_interval, min_score=bot.min_score,
        directions=[d.value for d in directions], generated_at=datetime.now(UTC),
        boards=boards, evidence=ASSET_EVIDENCE.get(asset_class, []),
    )


@router.get("/bots/{bot_id}", response_model=BotResponse)
async def get_autotrade_bot(
    bot_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BotResponse:
    return BotResponse.from_row(await _load_owned_bot(session, bot_id, current_user))


@router.post("/bots/{bot_id}/approve", response_model=BotResponse)
async def approve_autotrade_bot(
    bot_id: uuid.UUID,
    current_user: User = Depends(require_permission(Permission.STRATEGY_APPROVE_DEPLOYMENT)),
    session: AsyncSession = Depends(get_session),
) -> BotResponse:
    bot = await _load_owned_bot(session, bot_id, current_user)
    try:
        await approve_bot(bot, approved_by_user_id=current_user.id)
    except AutotradeError as exc:
        raise _autotrade_error(exc) from None
    await session.commit()
    await session.refresh(bot)
    return BotResponse.from_row(bot)


@router.post("/bots/{bot_id}/pause", response_model=BotResponse)
async def pause_autotrade_bot(
    bot_id: uuid.UUID,
    payload: PauseBotRequest | None = None,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
) -> BotResponse:
    bot = await _load_owned_bot(session, bot_id, current_user)
    try:
        await pause_bot(bot, reason=payload.reason if payload else None)
    except AutotradeError as exc:
        raise _autotrade_error(exc) from None
    await session.commit()
    await session.refresh(bot)
    return BotResponse.from_row(bot)


@router.post("/bots/{bot_id}/resume", response_model=BotResponse)
async def resume_autotrade_bot(
    bot_id: uuid.UUID,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
) -> BotResponse:
    bot = await _load_owned_bot(session, bot_id, current_user)
    try:
        await resume_bot(bot)
    except AutotradeError as exc:
        raise _autotrade_error(exc) from None
    await session.commit()
    await session.refresh(bot)
    return BotResponse.from_row(bot)


@router.post("/bots/{bot_id}/stop", response_model=BotResponse)
async def stop_autotrade_bot(
    bot_id: uuid.UUID,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
) -> BotResponse:
    bot = await _load_owned_bot(session, bot_id, current_user)
    try:
        await stop_bot(bot)
    except AutotradeError as exc:
        raise _autotrade_error(exc) from None
    await session.commit()
    await session.refresh(bot)
    return BotResponse.from_row(bot)


@router.post("/bots/{bot_id}/run", response_model=RunNowResponse)
async def run_autotrade_bot_now(
    bot_id: uuid.UUID,
    request: Request,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
) -> RunNowResponse:
    """One cycle for this bot, now, outside the timer. Runs the exact same
    engine the runner runs, under the same lock, and writes the same run
    row — a non-active bot gets its SKIPPED_NOT_ACTIVE row, a vendor-less
    deployment its SKIPPED_NOT_CONFIGURED row."""
    await _load_owned_bot(session, bot_id, current_user)
    runner = getattr(request.app.state, "autotrade_bot_runner", None)
    if runner is not None:
        result = await runner.run_once(only_bot_id=bot_id)
    else:
        result = await run_autotrade_cycle(
            get_session_factory(),
            settings=get_settings(),
            bar_router=getattr(request.app.state, "market_data_bar_backfill_provider", None),
            news_provider=getattr(request.app.state, "news_provider", None),
            only_bot_id=bot_id,
        )
    outcome = result.outcomes[0] if result.outcomes else None
    return RunNowResponse(
        status=result.status.value,
        detail=result.detail,
        run_status=outcome.status.value if outcome else None,
        run_detail=outcome.detail if outcome else None,
        symbols_scanned=outcome.symbols_scanned if outcome else 0,
        signals_found=outcome.signals_found if outcome else 0,
        trades_opened=outcome.trades_opened if outcome else 0,
        trades_closed=outcome.trades_closed if outcome else 0,
    )


@router.get("/bots/{bot_id}/runs", response_model=ListBotRunsResponse)
async def list_autotrade_bot_runs(
    bot_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListBotRunsResponse:
    await _load_owned_bot(session, bot_id, current_user)
    rows = (
        (
            await session.execute(
                select(AutotradeBotRun)
                .where(AutotradeBotRun.bot_id == bot_id)
                .order_by(AutotradeBotRun.started_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListBotRunsResponse(
        runs=[
            BotRunResponse(
                id=r.id,
                status=r.status.value,
                started_at=r.started_at,
                completed_at=r.completed_at,
                symbols_scanned=r.symbols_scanned,
                signals_found=r.signals_found,
                signals_skipped=r.signals_skipped,
                trades_opened=r.trades_opened,
                trades_closed=r.trades_closed,
                setups_active=list(r.setups_active or []),
                detail=r.detail,
            )
            for r in rows
        ],
        limit=limit,
        offset=offset,
    )


@router.get("/bots/{bot_id}/trades", response_model=ListBotTradesResponse)
async def list_autotrade_bot_trades(
    bot_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListBotTradesResponse:
    await _load_owned_bot(session, bot_id, current_user)
    rows = (
        (
            await session.execute(
                select(AutotradeBotTrade)
                .where(AutotradeBotTrade.bot_id == bot_id)
                .order_by(AutotradeBotTrade.opened_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListBotTradesResponse(
        trades=[
            BotTradeResponse(
                id=t.id,
                symbol=t.symbol,
                session_date=t.session_date,
                setup_name=t.setup_name,
                score=t.score,
                evidence=dict(t.evidence or {}),
                quantity=t.quantity,
                entry_price=t.entry_price,
                initial_stop_price=t.initial_stop_price,
                stop_price=t.stop_price,
                take_profit_price=t.take_profit_price,
                peak_price=t.peak_price,
                take_profit_armed=t.take_profit_armed,
                opened_at=t.opened_at,
                closed_at=t.closed_at,
                exit_price=t.exit_price,
                exit_reason=t.exit_reason,
                realized_pnl=t.realized_pnl,
                r_multiple=t.r_multiple,
                entry_order_id=t.entry_order_id,
                exit_order_id=t.exit_order_id,
            )
            for t in rows
        ],
        limit=limit,
        offset=offset,
    )


@router.get("/bots/{bot_id}/stats", response_model=BotStatsResponse)
async def get_autotrade_bot_stats(
    bot_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BotStatsResponse:
    bot = await _load_owned_bot(session, bot_id, current_user)
    history = await closed_trades(session, bot.id)
    stats = stats_by_setup(history)
    active = active_setups(list(bot.setups), strategy_mode=bot.strategy_mode, stats=stats)

    def _row(s) -> SetupStatsResponse:
        return SetupStatsResponse(
            setup_name=s.setup_name,
            trades=s.trades,
            wins=s.wins,
            win_rate=s.win_rate,
            expectancy_r=s.expectancy_r,
            total_r=s.total_r,
            total_pnl=s.total_pnl,
            demoted=s.demoted,
            symbol=s.symbol,
            hour=s.hour,
            avg_mfe_r=s.avg_mfe_r,
            avg_mae_r=s.avg_mae_r,
        )

    return BotStatsResponse(
        bot_id=bot.id,
        strategy_mode=bot.strategy_mode,
        configured_setups=list(bot.setups),
        active_setups=active,
        setups=[_row(s) for s in stats],
        by_symbol=[_row(s) for s in stats_by_setup_symbol(history)],
        by_hour=[_row(s) for s in stats_by_setup_hour(history)],
        closed_trades=sum(s.trades for s in stats),
        total_r=sum((s.total_r for s in stats), Decimal(0)),
        total_pnl=sum((s.total_pnl for s in stats), Decimal(0)),
    )


@router.get("/bots/{bot_id}/insights", response_model=ListBotInsightsResponse)
async def list_autotrade_bot_insights(
    bot_id: uuid.UUID,
    limit: int = Query(default=30, ge=1, le=MAX_LIST_LIMIT),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListBotInsightsResponse:
    """Phase 83 (D099): the bot's journal — one row per session, newest
    first, each with the day's aggregate and the findings the loop
    recorded for the operator to act on."""
    await _load_owned_bot(session, bot_id, current_user)
    rows = (
        (
            await session.execute(
                select(AutotradeBotInsight)
                .where(AutotradeBotInsight.bot_id == bot_id)
                .order_by(AutotradeBotInsight.session_date.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return ListBotInsightsResponse(
        insights=[
            BotInsightResponse(
                id=r.id,
                session_date=r.session_date,
                trades=r.trades,
                wins=r.wins,
                total_r=r.total_r,
                total_pnl=r.total_pnl,
                best_setup=r.best_setup,
                worst_setup=r.worst_setup,
                findings=list(r.findings or []),
                demoted_setups=list(r.demoted_setups or []),
                created_at=r.created_at,
            )
            for r in rows
        ]
    )
