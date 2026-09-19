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

from apps.api.app.auth.dependencies import get_current_user, require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.autotrade.learning import active_setups, setup_stats
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
    AutotradeBotRun,
    AutotradeBotTrade,
    User,
)

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

    @classmethod
    def from_row(cls, b: AutotradeBot) -> BotResponse:
        return cls(
            id=b.id, name=b.name, broker_id=b.broker_id, status=b.status.value,
            watchlist_id=b.watchlist_id, symbols=list(b.symbols), market_type=b.market_type,
            bar_interval=b.bar_interval, max_trades_per_session=b.max_trades_per_session,
            max_trades_per_day=b.max_trades_per_day, capital_per_trade=b.capital_per_trade,
            strategy_mode=b.strategy_mode, setups=list(b.setups), min_score=b.min_score,
            stop_loss_mode=b.stop_loss_mode, stop_loss_max_pct=b.stop_loss_max_pct,
            trailing_stop_pct=b.trailing_stop_pct, take_profit_mode=b.take_profit_mode,
            take_profit_min_pct=b.take_profit_min_pct,
            trailing_take_profit_pct=b.trailing_take_profit_pct,
            news_blackout_minutes=b.news_blackout_minutes, approved_at=b.approved_at,
            paused_reason=b.paused_reason, stopped_at=b.stopped_at,
            last_evaluated_at=b.last_evaluated_at, created_at=b.created_at,
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


class BotStatsResponse(BaseModel):
    bot_id: uuid.UUID
    strategy_mode: str
    configured_setups: list[str]
    active_setups: list[str]
    setups: list[SetupStatsResponse]
    closed_trades: int
    total_r: Decimal
    total_pnl: Decimal


class RunNowResponse(BaseModel):
    status: str
    detail: str | None
    run_status: str | None
    run_detail: str | None
    symbols_scanned: int
    signals_found: int
    trades_opened: int
    trades_closed: int


class SetupsResponse(BaseModel):
    setups: list[str]


# --- helpers ---------------------------------------------------------------


async def _load_owned_bot(
    session: AsyncSession, bot_id: uuid.UUID, user: User
) -> AutotradeBot:
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
        name=payload.name, broker_id=payload.broker_id, watchlist_id=payload.watchlist_id,
        symbols=payload.symbols, market_type=payload.market_type,
        bar_interval=payload.bar_interval,
        max_trades_per_session=payload.max_trades_per_session,
        max_trades_per_day=payload.max_trades_per_day,
        capital_per_trade=payload.capital_per_trade, strategy_mode=payload.strategy_mode,
        setups=payload.setups, min_score=payload.min_score,
        stop_loss_mode=payload.stop_loss_mode, stop_loss_max_pct=payload.stop_loss_max_pct,
        trailing_stop_pct=payload.trailing_stop_pct, take_profit_mode=payload.take_profit_mode,
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
        await session.execute(
            select(AutotradeBot)
            .where(AutotradeBot.owner_user_id == current_user.id)
            .order_by(AutotradeBot.created_at.desc())
        )
    ).scalars().all()
    return ListBotsResponse(bots=[BotResponse.from_row(b) for b in rows])


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
        await session.execute(
            select(AutotradeBotRun)
            .where(AutotradeBotRun.bot_id == bot_id)
            .order_by(AutotradeBotRun.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return ListBotRunsResponse(
        runs=[
            BotRunResponse(
                id=r.id, status=r.status.value, started_at=r.started_at,
                completed_at=r.completed_at, symbols_scanned=r.symbols_scanned,
                signals_found=r.signals_found, signals_skipped=r.signals_skipped,
                trades_opened=r.trades_opened, trades_closed=r.trades_closed,
                setups_active=list(r.setups_active or []), detail=r.detail,
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
        await session.execute(
            select(AutotradeBotTrade)
            .where(AutotradeBotTrade.bot_id == bot_id)
            .order_by(AutotradeBotTrade.opened_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return ListBotTradesResponse(
        trades=[
            BotTradeResponse(
                id=t.id, symbol=t.symbol, session_date=t.session_date,
                setup_name=t.setup_name, score=t.score, evidence=dict(t.evidence or {}),
                quantity=t.quantity, entry_price=t.entry_price,
                initial_stop_price=t.initial_stop_price, stop_price=t.stop_price,
                take_profit_price=t.take_profit_price, peak_price=t.peak_price,
                take_profit_armed=t.take_profit_armed, opened_at=t.opened_at,
                closed_at=t.closed_at, exit_price=t.exit_price, exit_reason=t.exit_reason,
                realized_pnl=t.realized_pnl, r_multiple=t.r_multiple,
                entry_order_id=t.entry_order_id, exit_order_id=t.exit_order_id,
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
    stats = await setup_stats(session, bot.id)
    active = active_setups(list(bot.setups), strategy_mode=bot.strategy_mode, stats=stats)
    return BotStatsResponse(
        bot_id=bot.id,
        strategy_mode=bot.strategy_mode,
        configured_setups=list(bot.setups),
        active_setups=active,
        setups=[
            SetupStatsResponse(
                setup_name=s.setup_name, trades=s.trades, wins=s.wins, win_rate=s.win_rate,
                expectancy_r=s.expectancy_r, total_r=s.total_r, total_pnl=s.total_pnl,
                demoted=s.demoted,
            )
            for s in stats
        ],
        closed_trades=sum(s.trades for s in stats),
        total_r=sum((s.total_r for s in stats), Decimal(0)),
        total_pnl=sum((s.total_pnl for s in stats), Decimal(0)),
    )
