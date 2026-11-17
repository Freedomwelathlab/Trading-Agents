"""Strategy Library paper bot routes (Phase 109, D136).

    POST   /library/bots                 create (PENDING_APPROVAL)   strategy:deploy
    GET    /library/bots                 the caller's bots
    GET    /library/bots/{id}
    POST   /library/bots/{id}/approve    -> ACTIVE       strategy:approve_deployment
    POST   /library/bots/{id}/pause | resume | stop      strategy:deploy
    POST   /library/bots/{id}/run        one cycle now
    POST   /library/bots/{id}/delete     only a bot that holds nothing
    GET    /library/bots/{id}/runs

Permissions are the ones every other robot here uses: creating one is
`strategy:deploy`, approving one is the separate
`strategy:approve_deployment`. Ownership is enforced on every read and
action. Paper brokers only.

"Run now" works with the scheduled loop off (`LIBRARY_BOT_RUNNER_ENABLED`,
default false): the switch gates the unattended loop, not a person
pressing a button on a bot somebody approved.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.auth.dependencies import get_current_user, require_permission
from apps.api.app.auth.permissions import Permission
from apps.api.app.core.config import get_settings
from apps.api.app.db.base import get_session, get_session_factory
from apps.api.app.db.models import LibraryBot, LibraryBotRun, LibraryBotStatus, User
from apps.api.app.library.bot import QuoteProvider
from apps.api.app.library.bot_runner import run_library_bot_cycle_all
from apps.api.app.library.bot_service import (
    LibraryBotError,
    LibraryBotSpec,
    approve_library_bot,
    create_library_bot,
    pause_library_bot,
    resume_library_bot,
    stop_library_bot,
)
from apps.api.app.library.service import MAX_SYMBOLS, DailyHistoryProvider
from apps.api.app.marketdata.providers.yahoo import YahooDailyProvider
from apps.api.app.notifications.alerts import AlertSink, get_alert_sink

router = APIRouter(prefix="/library/bots", tags=["library", "bots"])

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 500

_FEED: YahooDailyProvider | None = None


def get_bot_feeds() -> tuple[DailyHistoryProvider | None, QuoteProvider | None]:
    """History and quotes for a bot cycle; `(None, None)` when the library's
    data source is switched off, which the cycle records as NOT_CONFIGURED."""
    global _FEED
    if get_settings().library_data_provider == "none":
        return None, None
    if _FEED is None:
        _FEED = YahooDailyProvider()
    return _FEED, _FEED


class CreateLibraryBotRequest(BaseModel):
    name: str = Field(default="", max_length=80)
    broker_id: uuid.UUID
    strategy_id: str = Field(min_length=1, max_length=64)
    symbols: list[str] = Field(min_length=1, max_length=MAX_SYMBOLS)
    params: dict[str, float] = Field(default_factory=dict)
    capital: Decimal = Field(gt=0)
    max_drawdown_pct: Decimal | None = Field(default=None, gt=0)


class LibraryBotResponse(BaseModel):
    id: uuid.UUID
    name: str
    broker_id: uuid.UUID
    status: str
    strategy_id: str
    symbols: list[str]
    params: dict[str, Any]
    capital: Decimal
    max_drawdown_pct: Decimal | None
    cash: Decimal
    positions: dict[str, str]
    target_weights: dict[str, float] | None
    target_explain: dict[str, str]
    target_refused: dict[str, str]
    data_as_of: str | None
    last_decision_date: date | None
    last_equity: Decimal | None
    peak_equity: Decimal | None
    return_pct: Decimal | None
    """Last valued equity against the starting capital. NULL until the bot
    has completed a cycle - never reported as zero before then."""
    approved_at: datetime | None
    paused_reason: str | None
    stopped_at: datetime | None
    last_evaluated_at: datetime | None
    created_at: datetime

    @classmethod
    def of(cls, bot: LibraryBot) -> LibraryBotResponse:
        target = bot.target or {}
        return_pct = None
        if bot.last_equity is not None and bot.capital:
            return_pct = (Decimal(bot.last_equity) / Decimal(bot.capital) - 1) * 100
        return cls(
            id=bot.id,
            name=bot.name,
            broker_id=bot.broker_id,
            status=bot.status.value,
            strategy_id=bot.strategy_id,
            symbols=list(bot.symbols),
            params=dict(bot.params or {}),
            capital=bot.capital,
            max_drawdown_pct=bot.max_drawdown_pct,
            cash=bot.cash,
            positions={s: str(q) for s, q in (bot.positions or {}).items()},
            target_weights=target.get("weights") if bot.target is not None else None,
            target_explain=dict(target.get("explain", {})),
            target_refused=dict(target.get("refused", {})),
            data_as_of=target.get("as_of"),
            last_decision_date=bot.last_decision_date,
            last_equity=bot.last_equity,
            peak_equity=bot.peak_equity,
            return_pct=return_pct,
            approved_at=bot.approved_at,
            paused_reason=bot.paused_reason,
            stopped_at=bot.stopped_at,
            last_evaluated_at=bot.last_evaluated_at,
            created_at=bot.created_at,
        )


class ListLibraryBotsResponse(BaseModel):
    items: list[LibraryBotResponse]
    limit: int
    offset: int


class LibraryBotRunResponse(BaseModel):
    id: uuid.UUID
    status: str
    started_at: datetime
    completed_at: datetime | None
    orders_filled: int
    orders_rejected: int
    equity: Decimal | None
    decision_date: date | None
    target: dict[str, float] | None
    orders: list[dict[str, Any]]
    detail: str | None


class ListLibraryBotRunsResponse(BaseModel):
    items: list[LibraryBotRunResponse]
    limit: int
    offset: int


class RunNowResponse(BaseModel):
    status: str
    run_status: str | None
    run_detail: str | None
    orders_filled: int
    orders_rejected: int
    equity: Decimal | None


class PauseRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class DeleteResponse(BaseModel):
    deleted: bool
    detail: str


async def _load_owned(session: AsyncSession, bot_id: uuid.UUID, user: User) -> LibraryBot:
    bot = await session.get(LibraryBot, bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"No library bot with id {bot_id}.")
    is_admin = user.role is not None and Permission.ADMIN.value in (user.role.permissions or [])
    if bot.owner_user_id != user.id and not is_admin:
        raise HTTPException(status_code=403, detail="This bot belongs to another account.")
    return bot


def _error(exc: LibraryBotError) -> HTTPException:
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


@router.post("", response_model=LibraryBotResponse, status_code=status.HTTP_201_CREATED)
async def create_bot(
    payload: CreateLibraryBotRequest,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
) -> LibraryBotResponse:
    spec = LibraryBotSpec(
        name=payload.name,
        broker_id=payload.broker_id,
        strategy_id=payload.strategy_id,
        symbols=payload.symbols,
        params=dict(payload.params),
        capital=payload.capital,
        max_drawdown_pct=payload.max_drawdown_pct,
    )
    try:
        bot = await create_library_bot(session, spec, user_id=current_user.id)
    except LibraryBotError as exc:
        raise _error(exc) from None
    await session.commit()
    await session.refresh(bot)
    return LibraryBotResponse.of(bot)


@router.get("", response_model=ListLibraryBotsResponse)
async def list_bots(
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListLibraryBotsResponse:
    rows = (
        (
            await session.execute(
                select(LibraryBot)
                .where(LibraryBot.owner_user_id == current_user.id)
                .order_by(LibraryBot.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListLibraryBotsResponse(
        items=[LibraryBotResponse.of(b) for b in rows], limit=limit, offset=offset
    )


@router.get("/{bot_id}", response_model=LibraryBotResponse)
async def get_bot(
    bot_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> LibraryBotResponse:
    return LibraryBotResponse.of(await _load_owned(session, bot_id, current_user))


@router.post("/{bot_id}/approve", response_model=LibraryBotResponse)
async def approve_bot(
    bot_id: uuid.UUID,
    current_user: User = Depends(require_permission(Permission.STRATEGY_APPROVE_DEPLOYMENT)),
    session: AsyncSession = Depends(get_session),
) -> LibraryBotResponse:
    bot = await _load_owned(session, bot_id, current_user)
    try:
        await approve_library_bot(bot, approved_by_user_id=current_user.id)
    except LibraryBotError as exc:
        raise _error(exc) from None
    await session.commit()
    await session.refresh(bot)
    return LibraryBotResponse.of(bot)


@router.post("/{bot_id}/pause", response_model=LibraryBotResponse)
async def pause_bot(
    bot_id: uuid.UUID,
    payload: PauseRequest | None = None,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
) -> LibraryBotResponse:
    bot = await _load_owned(session, bot_id, current_user)
    try:
        await pause_library_bot(bot, reason=payload.reason if payload else None)
    except LibraryBotError as exc:
        raise _error(exc) from None
    await session.commit()
    await session.refresh(bot)
    return LibraryBotResponse.of(bot)


@router.post("/{bot_id}/resume", response_model=LibraryBotResponse)
async def resume_bot(
    bot_id: uuid.UUID,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
) -> LibraryBotResponse:
    bot = await _load_owned(session, bot_id, current_user)
    try:
        await resume_library_bot(bot)
    except LibraryBotError as exc:
        raise _error(exc) from None
    await session.commit()
    await session.refresh(bot)
    return LibraryBotResponse.of(bot)


@router.post("/{bot_id}/stop", response_model=LibraryBotResponse)
async def stop_bot(
    bot_id: uuid.UUID,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
) -> LibraryBotResponse:
    bot = await _load_owned(session, bot_id, current_user)
    try:
        await stop_library_bot(bot)
    except LibraryBotError as exc:
        raise _error(exc) from None
    await session.commit()
    await session.refresh(bot)
    return LibraryBotResponse.of(bot)


@router.post("/{bot_id}/run", response_model=RunNowResponse)
async def run_bot_now(
    bot_id: uuid.UUID,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
    feeds: tuple[DailyHistoryProvider | None, QuoteProvider | None] = Depends(get_bot_feeds),
    alerts: AlertSink | None = Depends(get_alert_sink),
) -> RunNowResponse:
    await _load_owned(session, bot_id, current_user)
    provider, quotes = feeds
    result = await run_library_bot_cycle_all(
        get_session_factory(),
        settings=get_settings(),
        provider=provider,
        quotes=quotes,
        only_bot_id=bot_id,
        alerts=alerts,
    )
    outcome = result.outcomes[0] if result.outcomes else None
    return RunNowResponse(
        status=result.status.value,
        run_status=outcome.status.value if outcome else None,
        run_detail=outcome.detail if outcome else None,
        orders_filled=outcome.orders_filled if outcome else 0,
        orders_rejected=outcome.orders_rejected if outcome else 0,
        equity=outcome.equity if outcome else None,
    )


@router.post("/{bot_id}/delete", response_model=DeleteResponse)
async def delete_bot(
    bot_id: uuid.UUID,
    current_user: User = Depends(require_permission(Permission.STRATEGY_DEPLOY)),
    session: AsyncSession = Depends(get_session),
) -> DeleteResponse:
    """Removes a bot and its run history. Refused while the bot is running
    or still holds anything - deleting the record of a position does not
    close the position."""
    bot = await _load_owned(session, bot_id, current_user)
    if bot.status in (LibraryBotStatus.ACTIVE, LibraryBotStatus.PAUSED):
        raise HTTPException(
            status_code=409,
            detail=f"BOT_RUNNING: bot is {bot.status.value}; stop it before deleting it.",
        )
    held = {s: q for s, q in (bot.positions or {}).items() if Decimal(str(q)) > 0}
    if held:
        raise HTTPException(
            status_code=409,
            detail="BOT_HOLDS_POSITIONS: the bot still records "
            + ", ".join(f"{q} {s}" for s, q in sorted(held.items()))
            + ". Close them on the trading desk first.",
        )
    await session.execute(delete(LibraryBotRun).where(LibraryBotRun.bot_id == bot.id))
    await session.delete(bot)
    await session.commit()
    return DeleteResponse(deleted=True, detail="deleted")


@router.get("/{bot_id}/runs", response_model=ListLibraryBotRunsResponse)
async def list_bot_runs(
    bot_id: uuid.UUID,
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListLibraryBotRunsResponse:
    await _load_owned(session, bot_id, current_user)
    rows = (
        (
            await session.execute(
                select(LibraryBotRun)
                .where(LibraryBotRun.bot_id == bot_id)
                .order_by(LibraryBotRun.started_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListLibraryBotRunsResponse(
        items=[
            LibraryBotRunResponse(
                id=r.id,
                status=r.status.value,
                started_at=r.started_at,
                completed_at=r.completed_at,
                orders_filled=r.orders_filled,
                orders_rejected=r.orders_rejected,
                equity=r.equity,
                decision_date=r.decision_date,
                target=r.target,
                orders=list(r.orders or []),
                detail=r.detail,
            )
            for r in rows
        ],
        limit=limit,
        offset=offset,
    )
