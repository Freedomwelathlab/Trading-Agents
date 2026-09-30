"""Options paper bot routes (Phase 102, D122).

    POST   /options-bots                  create (PENDING_APPROVAL)      strategy:deploy
    GET    /options-bots                  the caller's bots
    GET    /options-bots/structures       the structure types a bot may open
    GET    /options-bots/{id}
    POST   /options-bots/{id}/approve     -> ACTIVE          strategy:approve_deployment
    POST   /options-bots/{id}/pause
    POST   /options-bots/{id}/resume
    POST   /options-bots/{id}/stop
    POST   /options-bots/{id}/run         one cycle now (a non-active bot writes
                                          its SKIPPED_NOT_ACTIVE row)
    GET    /options-bots/{id}/runs
    GET    /options-bots/{id}/trades
    GET    /options-bots/{id}/stats       per-structure learning summary

Permissions reuse the autotrade / deployment ones deliberately (the same
class of act): creating a robot that places paper orders is
`strategy:deploy`; approving one is `strategy:approve_deployment`, a
separate permission so an organisation can require a second person.
Ownership is enforced on every read and action.

"Run now" is a person pressing a button on an approved bot and works with
the scheduled loop switched off (`OPTIONS_BOT_RUNNER_ENABLED=false`, the
default) - the switch gates the unattended loop, not a human action.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.auth.dependencies import get_current_user, require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.core.config import get_settings
from apps.api.app.db.base import get_session, get_session_factory
from apps.api.app.db.models import OptionsBot, OptionsBotRun, OptionsBotTrade, User
from apps.api.app.options_bot.learning import (
    StructureStats,
    closed_bot_trades,
    stats_by_exit_reason,
    stats_by_structure,
)
from apps.api.app.options_bot.runner import run_options_bot_cycle_all
from apps.api.app.options_bot.service import (
    BOT_STRUCTURES,
    OptionsBotError,
    OptionsBotSpec,
    approve_options_bot,
    create_options_bot,
    pause_options_bot,
    resume_options_bot,
    stop_options_bot,
)

router = APIRouter(prefix="/options-bots", tags=["options", "bots"])

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 500


class CreateOptionsBotRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    broker_id: uuid.UUID
    underlying: str = Field(min_length=1, max_length=32)
    structure_type: str
    target_delta: Decimal = Field(gt=0, lt=1)
    dte_min: int = Field(ge=0, le=365)
    dte_max: int = Field(ge=0, le=365)
    spread_width: Decimal | None = Field(default=None, gt=0)
    profit_target_pct: Decimal = Field(gt=0)
    stop_pct: Decimal = Field(gt=0)
    max_concurrent_positions: int = Field(ge=1, le=20)
    capital_per_trade: Decimal = Field(gt=0)


class PauseRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class OptionsBotResponse(BaseModel):
    id: uuid.UUID
    name: str
    broker_id: uuid.UUID
    status: str
    underlying: str
    structure_type: str
    target_delta: Decimal
    dte_min: int
    dte_max: int
    spread_width: Decimal | None
    profit_target_pct: Decimal
    stop_pct: Decimal
    max_concurrent_positions: int
    capital_per_trade: Decimal
    approved_at: datetime | None
    paused_reason: str | None
    stopped_at: datetime | None
    last_evaluated_at: datetime | None
    created_at: datetime

    @classmethod
    def from_row(cls, b: OptionsBot) -> OptionsBotResponse:
        return cls(
            id=b.id, name=b.name, broker_id=b.broker_id, status=b.status.value,
            underlying=b.underlying, structure_type=b.structure_type,
            target_delta=b.target_delta, dte_min=b.dte_min, dte_max=b.dte_max,
            spread_width=b.spread_width, profit_target_pct=b.profit_target_pct,
            stop_pct=b.stop_pct, max_concurrent_positions=b.max_concurrent_positions,
            capital_per_trade=b.capital_per_trade, approved_at=b.approved_at,
            paused_reason=b.paused_reason, stopped_at=b.stopped_at,
            last_evaluated_at=b.last_evaluated_at, created_at=b.created_at,
        )


class ListOptionsBotsResponse(BaseModel):
    bots: list[OptionsBotResponse]


class OptionsBotRunResponse(BaseModel):
    id: uuid.UUID
    status: str
    started_at: datetime
    completed_at: datetime | None
    positions_checked: int
    trades_opened: int
    trades_closed: int
    detail: str | None


class ListOptionsBotRunsResponse(BaseModel):
    runs: list[OptionsBotRunResponse]
    limit: int
    offset: int


class OptionsBotTradeResponse(BaseModel):
    id: uuid.UUID
    structure_id: uuid.UUID
    structure_type: str
    underlying: str
    expiry: date
    quantity: int
    target_delta: Decimal
    entry_delta: Decimal | None
    entry_net_price: Decimal
    max_loss: Decimal
    profit_target_pct: Decimal
    stop_pct: Decimal
    peak_pnl_pct: Decimal | None
    trough_pnl_pct: Decimal | None
    opened_at: datetime
    closed_at: datetime | None
    exit_net_price: Decimal | None
    exit_reason: str | None
    realized_pnl: Decimal | None
    return_on_risk: Decimal | None


class ListOptionsBotTradesResponse(BaseModel):
    trades: list[OptionsBotTradeResponse]
    limit: int
    offset: int


class StructureStatsResponse(BaseModel):
    key: str
    trades: int
    wins: int
    win_rate: Decimal | None
    expectancy: Decimal | None
    expectancy_on_risk: Decimal | None
    total_pnl: Decimal
    avg_peak_pnl_pct: Decimal | None
    avg_trough_pnl_pct: Decimal | None


class OptionsBotStatsResponse(BaseModel):
    bot_id: uuid.UUID
    closed_trades: int
    total_pnl: Decimal
    by_structure: list[StructureStatsResponse]
    by_exit_reason: list[StructureStatsResponse]


class RunNowResponse(BaseModel):
    status: str
    run_status: str | None
    run_detail: str | None
    positions_checked: int
    trades_opened: int
    trades_closed: int


class StructuresResponse(BaseModel):
    structures: list[str]


def _stats_out(s: StructureStats) -> StructureStatsResponse:
    return StructureStatsResponse(
        key=s.key, trades=s.trades, wins=s.wins, win_rate=s.win_rate, expectancy=s.expectancy,
        expectancy_on_risk=s.expectancy_on_risk, total_pnl=s.total_pnl,
        avg_peak_pnl_pct=s.avg_peak_pnl_pct, avg_trough_pnl_pct=s.avg_trough_pnl_pct,
    )


async def _load_owned(session: AsyncSession, bot_id: uuid.UUID, user: User) -> OptionsBot:
    bot = await session.get(OptionsBot, bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"No options bot with id {bot_id}.")
    is_admin = user.role is not None and Permission.ADMIN.value in (user.role.permissions or [])
    if bot.owner_user_id != user.id and not is_admin:
        raise HTTPException(status_code=403, detail="This bot belongs to another account.")
    return bot


def _error(exc: OptionsBotError) -> HTTPException:
    code = str(exc).split(":", 1)[0]
    return HTTPException(
        status_code={
            "NO_SUCH_BROKER": 404,
            "NO_BROKER_GRANT": 403,
            "NOT_PENDING_APPROVAL": 409,
            "NOT_ACTIVE": 409,
            "NOT_PAUSED": 409,
            "ALREADY_STOPPED": 409,
        }.get(code, 400),
        detail=str(exc),
    )


@router.get("/structures", response_model=StructuresResponse)
async def list_bot_structures(_: User = Depends(get_current_user)) -> StructuresResponse:
    return StructuresResponse(structures=[s.value for s in BOT_STRUCTURES])


@router.post("", response_model=OptionsBotResponse, status_code=status.HTTP_201_CREATED)
async def create_bot(
    payload: CreateOptionsBotRequest,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
) -> OptionsBotResponse:
    spec = OptionsBotSpec(
        name=payload.name, broker_id=payload.broker_id, underlying=payload.underlying,
        structure_type=payload.structure_type, target_delta=payload.target_delta,
        dte_min=payload.dte_min, dte_max=payload.dte_max, spread_width=payload.spread_width,
        profit_target_pct=payload.profit_target_pct, stop_pct=payload.stop_pct,
        max_concurrent_positions=payload.max_concurrent_positions,
        capital_per_trade=payload.capital_per_trade,
    )
    try:
        bot = await create_options_bot(session, spec, user_id=current_user.id)
    except OptionsBotError as exc:
        raise _error(exc) from None
    await session.commit()
    await session.refresh(bot)
    return OptionsBotResponse.from_row(bot)


@router.get("", response_model=ListOptionsBotsResponse)
async def list_bots(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListOptionsBotsResponse:
    rows = (
        await session.execute(
            select(OptionsBot)
            .where(OptionsBot.owner_user_id == current_user.id)
            .order_by(OptionsBot.created_at.desc())
        )
    ).scalars().all()
    return ListOptionsBotsResponse(bots=[OptionsBotResponse.from_row(b) for b in rows])


@router.get("/{bot_id}", response_model=OptionsBotResponse)
async def get_bot(
    bot_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> OptionsBotResponse:
    return OptionsBotResponse.from_row(await _load_owned(session, bot_id, current_user))


@router.post("/{bot_id}/approve", response_model=OptionsBotResponse)
async def approve_bot(
    bot_id: uuid.UUID,
    current_user: User = Depends(require_permission(Permission.STRATEGY_APPROVE_DEPLOYMENT)),
    session: AsyncSession = Depends(get_session),
) -> OptionsBotResponse:
    bot = await _load_owned(session, bot_id, current_user)
    try:
        await approve_options_bot(bot, approved_by_user_id=current_user.id)
    except OptionsBotError as exc:
        raise _error(exc) from None
    await session.commit()
    await session.refresh(bot)
    return OptionsBotResponse.from_row(bot)


@router.post("/{bot_id}/pause", response_model=OptionsBotResponse)
async def pause_bot(
    bot_id: uuid.UUID,
    payload: PauseRequest | None = None,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
) -> OptionsBotResponse:
    bot = await _load_owned(session, bot_id, current_user)
    try:
        await pause_options_bot(bot, reason=payload.reason if payload else None)
    except OptionsBotError as exc:
        raise _error(exc) from None
    await session.commit()
    await session.refresh(bot)
    return OptionsBotResponse.from_row(bot)


@router.post("/{bot_id}/resume", response_model=OptionsBotResponse)
async def resume_bot(
    bot_id: uuid.UUID,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
) -> OptionsBotResponse:
    bot = await _load_owned(session, bot_id, current_user)
    try:
        await resume_options_bot(bot)
    except OptionsBotError as exc:
        raise _error(exc) from None
    await session.commit()
    await session.refresh(bot)
    return OptionsBotResponse.from_row(bot)


@router.post("/{bot_id}/stop", response_model=OptionsBotResponse)
async def stop_bot(
    bot_id: uuid.UUID,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
) -> OptionsBotResponse:
    bot = await _load_owned(session, bot_id, current_user)
    try:
        await stop_options_bot(bot)
    except OptionsBotError as exc:
        raise _error(exc) from None
    await session.commit()
    await session.refresh(bot)
    return OptionsBotResponse.from_row(bot)


@router.post("/{bot_id}/run", response_model=RunNowResponse)
async def run_bot_now(
    bot_id: uuid.UUID,
    request: Request,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
) -> RunNowResponse:
    await _load_owned(session, bot_id, current_user)
    runner = getattr(request.app.state, "options_bot_runner", None)
    if runner is not None:
        result = await runner.run_once(only_bot_id=bot_id)
    else:
        result = await run_options_bot_cycle_all(
            get_session_factory(),
            settings=get_settings(),
            provider=getattr(request.app.state, "option_chain_provider", None),
            bar_router=getattr(request.app.state, "market_data_bar_backfill_provider", None),
            only_bot_id=bot_id,
        )
    outcome = result.outcomes[0] if result.outcomes else None
    return RunNowResponse(
        status=result.status.value,
        run_status=outcome.status.value if outcome else None,
        run_detail=outcome.detail if outcome else None,
        positions_checked=outcome.positions_checked if outcome else 0,
        trades_opened=outcome.trades_opened if outcome else 0,
        trades_closed=outcome.trades_closed if outcome else 0,
    )


@router.get("/{bot_id}/runs", response_model=ListOptionsBotRunsResponse)
async def list_bot_runs(
    bot_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListOptionsBotRunsResponse:
    await _load_owned(session, bot_id, current_user)
    rows = (
        await session.execute(
            select(OptionsBotRun)
            .where(OptionsBotRun.bot_id == bot_id)
            .order_by(OptionsBotRun.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return ListOptionsBotRunsResponse(
        runs=[
            OptionsBotRunResponse(
                id=r.id, status=r.status.value, started_at=r.started_at,
                completed_at=r.completed_at, positions_checked=r.positions_checked,
                trades_opened=r.trades_opened, trades_closed=r.trades_closed, detail=r.detail,
            )
            for r in rows
        ],
        limit=limit,
        offset=offset,
    )


@router.get("/{bot_id}/trades", response_model=ListOptionsBotTradesResponse)
async def list_bot_trades(
    bot_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListOptionsBotTradesResponse:
    await _load_owned(session, bot_id, current_user)
    rows = (
        await session.execute(
            select(OptionsBotTrade)
            .where(OptionsBotTrade.bot_id == bot_id)
            .order_by(OptionsBotTrade.opened_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return ListOptionsBotTradesResponse(
        trades=[
            OptionsBotTradeResponse(
                id=t.id, structure_id=t.structure_id, structure_type=t.structure_type,
                underlying=t.underlying, expiry=t.expiry, quantity=t.quantity,
                target_delta=t.target_delta, entry_delta=t.entry_delta,
                entry_net_price=t.entry_net_price, max_loss=t.max_loss,
                profit_target_pct=t.profit_target_pct, stop_pct=t.stop_pct,
                peak_pnl_pct=t.peak_pnl_pct, trough_pnl_pct=t.trough_pnl_pct,
                opened_at=t.opened_at, closed_at=t.closed_at, exit_net_price=t.exit_net_price,
                exit_reason=t.exit_reason, realized_pnl=t.realized_pnl,
                return_on_risk=t.return_on_risk,
            )
            for t in rows
        ],
        limit=limit,
        offset=offset,
    )


@router.get("/{bot_id}/stats", response_model=OptionsBotStatsResponse)
async def get_bot_stats(
    bot_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> OptionsBotStatsResponse:
    bot = await _load_owned(session, bot_id, current_user)
    history = await closed_bot_trades(session, bot.id)
    by_structure = stats_by_structure(history)
    return OptionsBotStatsResponse(
        bot_id=bot.id,
        closed_trades=sum(s.trades for s in by_structure),
        total_pnl=sum((s.total_pnl for s in by_structure), Decimal(0)),
        by_structure=[_stats_out(s) for s in by_structure],
        by_exit_reason=[_stats_out(s) for s in stats_by_exit_reason(history)],
    )
