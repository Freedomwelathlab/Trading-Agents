"""Autotrade Bot lifecycle (Phase 81, D098).

Mirrors `deployments/service.py` on purpose: create -> PENDING_APPROVAL;
an explicit, separately-permissioned approval -> ACTIVE; pause/resume as
the reversible operational switch; stop as terminal. There is no path to
ACTIVE that is not an approval action.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.backtesting.setups import SETUPS
from apps.api.app.db.models import (
    AutotradeBot,
    AutotradeBotStatus,
    Broker,
    BrokerGrant,
    Watchlist,
    WatchlistItem,
)

MARKET_TYPES = ("auto", "regular", "pre_market", "post_market")
STRATEGY_MODES = ("auto", "single", "multi")
STOP_MODES = ("auto", "max")
TAKE_PROFIT_MODES = ("auto", "min")
BAR_INTERVALS = ("1m", "5m", "15m", "30m", "1h")


class AutotradeError(Exception):
    """Raised with a `CODE: human explanation` message the route maps to a
    4xx; the code prefix is what a client should switch on."""


@dataclass(frozen=True)
class BotSpec:
    name: str
    broker_id: uuid.UUID
    watchlist_id: uuid.UUID | None
    symbols: Sequence[str]
    market_type: str
    bar_interval: str
    max_trades_per_session: int
    max_trades_per_day: int
    capital_per_trade: Decimal
    strategy_mode: str
    setups: Sequence[str]
    min_score: int
    stop_loss_mode: str
    stop_loss_max_pct: Decimal | None
    trailing_stop_pct: Decimal | None
    take_profit_mode: str
    take_profit_min_pct: Decimal | None
    trailing_take_profit_pct: Decimal | None
    news_blackout_minutes: int
    # Phase 87 (D106). Defaulted, and therefore last: both are additions to
    # a spec every existing caller already builds positionally.
    extended_hours_min_score: int | None = None
    allow_short: bool = False


EXTENDED_HOURS_SCORE_PREMIUM = 2
"""How much higher an extended-hours signal must score than a regular one
when the operator did not name a figure (Phase 87, D106).

Not a tuned parameter - nothing has measured the right premium, and this
file would be the wrong place to claim one. It is a deliberate default in
the safe direction, chosen because extended-hours prints are thin: over
the window this platform's setups were built against, TQQQ traded roughly
1.9M shares pre-market to 53M in the regular session. The same score is
computed from materially less tape at 04:30, so the bar is raised until
somebody measures what it should be. An operator who disagrees passes
their own `extended_hours_min_score`.
"""


def _extended_hours_bar(spec: BotSpec) -> int | None:
    """None for a regular-session-only bot: the field would never be read,
    and storing a number that is never consulted invites someone to trust
    it later."""
    if spec.extended_hours_min_score is not None:
        return spec.extended_hours_min_score
    if spec.market_type in ("auto", "pre_market", "post_market"):
        return min(10, spec.min_score + EXTENDED_HOURS_SCORE_PREMIUM)
    return None


def _normalize_symbols(raw: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for s in raw:
        sym = s.strip().upper()
        if sym and sym not in seen:
            seen.append(sym)
    return seen


def _touch(bot: AutotradeBot) -> None:
    bot.updated_at = datetime.now(UTC)


async def create_bot(
    session: AsyncSession, spec: BotSpec, *, user_id: uuid.UUID
) -> AutotradeBot:
    """Validate every operator input against what the engine can honour,
    then persist the bot PENDING_APPROVAL. Refusals are specific."""
    broker = await session.get(Broker, spec.broker_id)
    if broker is None:
        raise AutotradeError(f"NO_SUCH_BROKER: no broker with id {spec.broker_id}.")
    grant = (
        await session.execute(
            select(BrokerGrant).where(
                BrokerGrant.user_id == user_id, BrokerGrant.broker_id == spec.broker_id
            )
        )
    ).scalar_one_or_none()
    if grant is None:
        raise AutotradeError(
            "NO_BROKER_GRANT: you hold no access grant for this broker; an admin "
            "grants it under Administration → Broker grants."
        )

    symbols = _normalize_symbols(spec.symbols)
    if spec.watchlist_id is not None:
        wl = await session.get(Watchlist, spec.watchlist_id)
        if wl is None or wl.user_id != user_id:
            raise AutotradeError(
                "NO_SUCH_WATCHLIST: that watchlist is not yours or does not exist."
            )
        if not symbols:
            items = (
                await session.execute(
                    select(WatchlistItem.symbol).where(WatchlistItem.watchlist_id == wl.id)
                )
            ).scalars().all()
            symbols = _normalize_symbols(items)
    if not symbols:
        raise AutotradeError("NO_SYMBOLS: pick at least one symbol (or a non-empty watchlist).")

    if spec.market_type not in MARKET_TYPES:
        raise AutotradeError(f"BAD_MARKET_TYPE: expected one of {MARKET_TYPES}.")
    if spec.bar_interval not in BAR_INTERVALS:
        raise AutotradeError(f"BAD_BAR_INTERVAL: expected one of {BAR_INTERVALS}.")
    if spec.strategy_mode not in STRATEGY_MODES:
        raise AutotradeError(f"BAD_STRATEGY_MODE: expected one of {STRATEGY_MODES}.")
    if spec.stop_loss_mode not in STOP_MODES:
        raise AutotradeError(f"BAD_STOP_MODE: expected one of {STOP_MODES}.")
    if spec.take_profit_mode not in TAKE_PROFIT_MODES:
        raise AutotradeError(f"BAD_TAKE_PROFIT_MODE: expected one of {TAKE_PROFIT_MODES}.")
    if spec.stop_loss_mode == "max" and (
        spec.stop_loss_max_pct is None or spec.stop_loss_max_pct <= 0
    ):
        raise AutotradeError("STOP_MAX_REQUIRED: stop-loss mode 'max' needs a positive percent.")
    if spec.take_profit_mode == "min" and (
        spec.take_profit_min_pct is None or spec.take_profit_min_pct <= 0
    ):
        raise AutotradeError("TP_MIN_REQUIRED: take-profit mode 'min' needs a positive percent.")
    for label, pct in (
        ("trailing_stop_pct", spec.trailing_stop_pct),
        ("trailing_take_profit_pct", spec.trailing_take_profit_pct),
        ("stop_loss_max_pct", spec.stop_loss_max_pct),
        ("take_profit_min_pct", spec.take_profit_min_pct),
    ):
        if pct is not None and not (0 < pct < 100):
            raise AutotradeError(f"BAD_PERCENT: {label} must be between 0 and 100 (exclusive).")
    if spec.max_trades_per_session < 1 or spec.max_trades_per_day < 1:
        raise AutotradeError("BAD_LIMITS: trade limits must be at least 1.")
    if spec.max_trades_per_session > spec.max_trades_per_day:
        raise AutotradeError(
            "BAD_LIMITS: trades in live at once cannot exceed trades per day."
        )
    if spec.capital_per_trade <= 0:
        raise AutotradeError("BAD_CAPITAL: capital per trade must be positive.")
    if spec.news_blackout_minutes < 0:
        raise AutotradeError("BAD_BLACKOUT: news blackout minutes cannot be negative.")

    if spec.strategy_mode == "auto":
        setups = sorted(SETUPS)
    else:
        setups = [s for s in spec.setups if s in SETUPS]
        unknown = [s for s in spec.setups if s not in SETUPS]
        if unknown:
            raise AutotradeError(
                f"UNKNOWN_SETUP: {unknown}; known setups are {sorted(SETUPS)}."
            )
        if not setups:
            raise AutotradeError("NO_SETUPS: pick at least one setup, or use strategy mode 'auto'.")
        if spec.strategy_mode == "single" and len(setups) != 1:
            raise AutotradeError(
                "SINGLE_MEANS_ONE: strategy mode 'single' takes exactly one setup."
            )

    bot = AutotradeBot(
        id=uuid.uuid4(),
        name=spec.name.strip() or "Autotrade bot",
        owner_user_id=user_id,
        broker_id=spec.broker_id,
        status=AutotradeBotStatus.PENDING_APPROVAL,
        watchlist_id=spec.watchlist_id,
        symbols=symbols,
        market_type=spec.market_type,
        bar_interval=spec.bar_interval,
        max_trades_per_session=spec.max_trades_per_session,
        max_trades_per_day=spec.max_trades_per_day,
        capital_per_trade=spec.capital_per_trade,
        strategy_mode=spec.strategy_mode,
        setups=setups,
        min_score=spec.min_score,
        extended_hours_min_score=_extended_hours_bar(spec),
        allow_short=spec.allow_short,
        stop_loss_mode=spec.stop_loss_mode,
        stop_loss_max_pct=spec.stop_loss_max_pct,
        trailing_stop_pct=spec.trailing_stop_pct,
        take_profit_mode=spec.take_profit_mode,
        take_profit_min_pct=spec.take_profit_min_pct,
        trailing_take_profit_pct=spec.trailing_take_profit_pct,
        news_blackout_minutes=spec.news_blackout_minutes,
        requested_by_user_id=user_id,
    )
    session.add(bot)
    await session.flush()
    return bot


async def approve_bot(bot: AutotradeBot, *, approved_by_user_id: uuid.UUID | None) -> AutotradeBot:
    if bot.status is not AutotradeBotStatus.PENDING_APPROVAL:
        raise AutotradeError(
            f"NOT_PENDING_APPROVAL: bot is {bot.status.value}; only a pending bot can be approved."
        )
    bot.status = AutotradeBotStatus.ACTIVE
    bot.approved_by_user_id = approved_by_user_id
    bot.approved_at = datetime.now(UTC)
    _touch(bot)
    return bot


async def pause_bot(bot: AutotradeBot, *, reason: str | None) -> AutotradeBot:
    if bot.status is not AutotradeBotStatus.ACTIVE:
        raise AutotradeError(
            f"NOT_ACTIVE: bot is {bot.status.value}; only an active bot can be paused."
        )
    bot.status = AutotradeBotStatus.PAUSED
    bot.paused_reason = reason
    _touch(bot)
    return bot


async def resume_bot(bot: AutotradeBot) -> AutotradeBot:
    if bot.status is not AutotradeBotStatus.PAUSED:
        raise AutotradeError(
            f"NOT_PAUSED: bot is {bot.status.value}; only a paused bot can resume."
        )
    bot.status = AutotradeBotStatus.ACTIVE
    bot.paused_reason = None
    _touch(bot)
    return bot


async def stop_bot(bot: AutotradeBot) -> AutotradeBot:
    """Terminal. Open positions are NOT liquidated here — stopping the
    robot and selling everything it holds are different decisions, and
    auto-selling into whatever prompted the stop is how a bad hour becomes
    a realized loss (the same reasoning as D087's breakers). The desk's
    ordinary sell path closes them."""
    if bot.status is AutotradeBotStatus.STOPPED:
        raise AutotradeError("ALREADY_STOPPED: bot is already stopped.")
    bot.status = AutotradeBotStatus.STOPPED
    bot.stopped_at = datetime.now(UTC)
    _touch(bot)
    return bot
