"""Creating and steering a Strategy Library paper bot (Phase 109, D136).

Same human gate as every other robot here: a bot is created PENDING, does
nothing until someone with the approval permission approves it, and can be
paused, resumed or stopped. Paper brokers only - a live broker is refused
at creation, before a row exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import Broker, BrokerGrant, BrokerKind, LibraryBot, LibraryBotStatus
from apps.api.app.library.service import LibraryError, normalise_symbols
from apps.api.app.library.strategies import BASELINE, get_strategy
from apps.api.app.marketdata.providers.yahoo import UnsupportedSymbolError, yahoo_symbol

MIN_CAPITAL = Decimal("100")
MAX_CAPITAL = Decimal("100000000")


class LibraryBotError(Exception):
    """`CODE: human explanation`; the route maps the code to a 4xx."""


@dataclass(frozen=True)
class LibraryBotSpec:
    name: str
    broker_id: uuid.UUID
    strategy_id: str
    symbols: list[str]
    params: dict[str, Any]
    capital: Decimal
    max_drawdown_pct: Decimal | None = None


def _touch(bot: LibraryBot) -> None:
    bot.updated_at = datetime.now(UTC)


async def create_library_bot(
    session: AsyncSession, spec: LibraryBotSpec, *, user_id: uuid.UUID
) -> LibraryBot:
    broker = await session.get(Broker, spec.broker_id)
    if broker is None:
        raise LibraryBotError(f"NO_SUCH_BROKER: no broker with id {spec.broker_id}.")
    grant = (
        await session.execute(
            select(BrokerGrant).where(
                BrokerGrant.user_id == user_id, BrokerGrant.broker_id == spec.broker_id
            )
        )
    ).scalar_one_or_none()
    if grant is None:
        raise LibraryBotError(
            "NO_BROKER_GRANT: you hold no access grant for this broker; an admin grants it "
            "under Administration → Broker grants."
        )
    if broker.kind is not BrokerKind.PAPER:
        raise LibraryBotError("LIVE_NOT_SUPPORTED: library bots run on PAPER brokers only (D136).")

    try:
        strategy = get_strategy(spec.strategy_id)
    except KeyError:
        raise LibraryBotError(f"BAD_STRATEGY: unknown strategy {spec.strategy_id!r}.") from None
    if strategy.id == BASELINE.id:
        raise LibraryBotError("BAD_STRATEGY: buy-and-hold is a benchmark, not a bot strategy.")
    try:
        symbols = normalise_symbols(spec.symbols)
        for symbol in symbols:
            yahoo_symbol(symbol)
        params = strategy.resolve_params(spec.params)
    except LibraryError as exc:
        raise LibraryBotError(f"BAD_SYMBOLS: {exc}") from None
    except UnsupportedSymbolError as exc:
        raise LibraryBotError(f"BAD_SYMBOLS: {exc}") from None
    except ValueError as exc:
        raise LibraryBotError(f"BAD_PARAMS: {exc}") from None
    if len(symbols) < strategy.min_symbols:
        raise LibraryBotError(
            f"BAD_SYMBOLS: {strategy.name} needs at least {strategy.min_symbols} symbols."
        )
    if not MIN_CAPITAL <= spec.capital <= MAX_CAPITAL:
        raise LibraryBotError(
            f"BAD_CAPITAL: capital must be between {MIN_CAPITAL} and {MAX_CAPITAL}."
        )
    if spec.max_drawdown_pct is not None and not Decimal(1) <= spec.max_drawdown_pct <= Decimal(90):
        raise LibraryBotError("BAD_LIMITS: the drawdown kill switch is 1-90 (percent).")

    bot = LibraryBot(
        id=uuid.uuid4(),
        name=spec.name.strip() or strategy.name,
        owner_user_id=user_id,
        broker_id=spec.broker_id,
        status=LibraryBotStatus.PENDING_APPROVAL,
        strategy_id=strategy.id,
        symbols=symbols,
        params=params,
        capital=spec.capital,
        max_drawdown_pct=spec.max_drawdown_pct,
        cash=spec.capital,
        positions={},
        requested_by_user_id=user_id,
    )
    session.add(bot)
    await session.flush()
    return bot


async def approve_library_bot(
    bot: LibraryBot, *, approved_by_user_id: uuid.UUID | None
) -> LibraryBot:
    if bot.status is not LibraryBotStatus.PENDING_APPROVAL:
        raise LibraryBotError(
            f"NOT_PENDING_APPROVAL: bot is {bot.status.value}; only a pending bot can be approved."
        )
    bot.status = LibraryBotStatus.ACTIVE
    bot.approved_by_user_id = approved_by_user_id
    bot.approved_at = datetime.now(UTC)
    _touch(bot)
    return bot


async def pause_library_bot(bot: LibraryBot, *, reason: str | None) -> LibraryBot:
    if bot.status is not LibraryBotStatus.ACTIVE:
        raise LibraryBotError(
            f"NOT_ACTIVE: bot is {bot.status.value}; only an active bot can be paused."
        )
    bot.status = LibraryBotStatus.PAUSED
    bot.paused_reason = reason
    _touch(bot)
    return bot


async def resume_library_bot(bot: LibraryBot) -> LibraryBot:
    """Resuming after the kill switch starts a fresh peak: the old one would
    trip the switch again on the first cycle."""
    if bot.status is not LibraryBotStatus.PAUSED:
        raise LibraryBotError(
            f"NOT_PAUSED: bot is {bot.status.value}; only a paused bot can resume."
        )
    bot.status = LibraryBotStatus.ACTIVE
    bot.paused_reason = None
    bot.peak_equity = bot.last_equity
    bot.target = None
    _touch(bot)
    return bot


async def stop_library_bot(bot: LibraryBot) -> LibraryBot:
    """Terminal. Positions are NOT sold here - stopping the robot and
    liquidating what it holds are different decisions (D087). They stay on
    the paper broker and can be closed by hand."""
    if bot.status is LibraryBotStatus.STOPPED:
        raise LibraryBotError("ALREADY_STOPPED: bot is already stopped.")
    bot.status = LibraryBotStatus.STOPPED
    bot.stopped_at = datetime.now(UTC)
    _touch(bot)
    return bot
