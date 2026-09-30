"""Options bot lifecycle (Phase 102, D122).

Mirrors `autotrade/service.py` on purpose: create -> PENDING_APPROVAL; an
explicit, separately-permissioned approval -> ACTIVE; pause/resume as the
reversible switch; stop as terminal. No code path produces an ACTIVE bot
without the approval action.

One refusal the autotrade bot makes at run time is made here at creation
as well: a bot on a `kind=live` broker cannot be created at all, because
option routing is paper only and there is nothing for such a bot to do.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import (
    Broker,
    BrokerGrant,
    BrokerKind,
    OptionsBot,
    OptionsBotStatus,
)
from apps.api.app.options.paper_orders import OptionStructureType

BOT_STRUCTURES: tuple[OptionStructureType, ...] = (
    OptionStructureType.LONG_CALL,
    OptionStructureType.LONG_PUT,
    OptionStructureType.BULL_CALL,
    OptionStructureType.BEAR_PUT,
    OptionStructureType.BULL_PUT,
    OptionStructureType.BEAR_CALL,
    OptionStructureType.IRON_CONDOR,
)
"""What the bot may open. The cash-secured put is available to a manual
order but not to the robot: its max loss is the whole strike, which a
`capital_per_trade` sized for spreads would rarely cover and which is a
different risk than the defined-risk structures the playbook automates."""

SINGLE_LEG = (OptionStructureType.LONG_CALL, OptionStructureType.LONG_PUT)
CREDIT = (
    OptionStructureType.BULL_PUT,
    OptionStructureType.BEAR_CALL,
    OptionStructureType.IRON_CONDOR,
)
MAX_DTE = 365
MAX_CONCURRENT = 20


class OptionsBotError(Exception):
    """`CODE: human explanation`; the route maps the code to a 4xx."""


@dataclass(frozen=True)
class OptionsBotSpec:
    name: str
    broker_id: uuid.UUID
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


def _touch(bot: OptionsBot) -> None:
    bot.updated_at = datetime.now(UTC)


async def create_options_bot(
    session: AsyncSession, spec: OptionsBotSpec, *, user_id: uuid.UUID
) -> OptionsBot:
    broker = await session.get(Broker, spec.broker_id)
    if broker is None:
        raise OptionsBotError(f"NO_SUCH_BROKER: no broker with id {spec.broker_id}.")
    grant = (
        await session.execute(
            select(BrokerGrant).where(
                BrokerGrant.user_id == user_id, BrokerGrant.broker_id == spec.broker_id
            )
        )
    ).scalar_one_or_none()
    if grant is None:
        raise OptionsBotError(
            "NO_BROKER_GRANT: you hold no access grant for this broker; an admin grants it "
            "under Administration → Broker grants."
        )
    if broker.kind is not BrokerKind.PAPER:
        raise OptionsBotError(
            "LIVE_NOT_SUPPORTED: the options bot runs on PAPER brokers only; option routing "
            "has no live path (D122)."
        )

    try:
        structure = OptionStructureType(spec.structure_type)
    except ValueError:
        raise OptionsBotError(
            f"BAD_STRUCTURE: expected one of {[s.value for s in BOT_STRUCTURES]}."
        ) from None
    if structure not in BOT_STRUCTURES:
        raise OptionsBotError(
            f"BAD_STRUCTURE: the bot opens {[s.value for s in BOT_STRUCTURES]}; "
            f"{structure.value} is manual-order only."
        )
    underlying = spec.underlying.strip().upper()
    if not underlying:
        raise OptionsBotError("NO_UNDERLYING: name the underlying, e.g. TQQQ.US.")
    if not Decimal(0) < spec.target_delta < Decimal(1):
        raise OptionsBotError("BAD_DELTA: target delta is a magnitude strictly between 0 and 1.")
    if spec.dte_min < 0 or spec.dte_max < spec.dte_min or spec.dte_max > MAX_DTE:
        raise OptionsBotError(
            f"BAD_DTE_BAND: need 0 <= dte_min <= dte_max <= {MAX_DTE}."
        )
    if structure in SINGLE_LEG:
        if spec.spread_width is not None:
            raise OptionsBotError("BAD_WIDTH: a single-leg structure takes no spread width.")
    elif spec.spread_width is None or spec.spread_width <= 0:
        raise OptionsBotError("WIDTH_REQUIRED: a spread needs a positive spread width.")
    if spec.profit_target_pct <= 0 or spec.stop_pct <= 0:
        raise OptionsBotError("BAD_PERCENT: profit target and stop must be positive percents.")
    if structure in CREDIT and spec.profit_target_pct >= 100:
        raise OptionsBotError(
            "BAD_PERCENT: a credit structure can keep at most 100% of the credit it collects; "
            "a profit target at or above 100% would never fire before expiry."
        )
    if spec.profit_target_pct > 1000 or spec.stop_pct > 1000:
        raise OptionsBotError("BAD_PERCENT: percents above 1000 are not meaningful here.")
    if not 1 <= spec.max_concurrent_positions <= MAX_CONCURRENT:
        raise OptionsBotError(f"BAD_LIMITS: max concurrent positions must be 1-{MAX_CONCURRENT}.")
    if spec.capital_per_trade <= 0:
        raise OptionsBotError("BAD_CAPITAL: capital per trade must be positive.")

    bot = OptionsBot(
        id=uuid.uuid4(),
        name=spec.name.strip() or "Options bot",
        owner_user_id=user_id,
        broker_id=spec.broker_id,
        status=OptionsBotStatus.PENDING_APPROVAL,
        underlying=underlying,
        structure_type=structure.value,
        target_delta=spec.target_delta,
        dte_min=spec.dte_min,
        dte_max=spec.dte_max,
        spread_width=spec.spread_width,
        profit_target_pct=spec.profit_target_pct,
        stop_pct=spec.stop_pct,
        max_concurrent_positions=spec.max_concurrent_positions,
        capital_per_trade=spec.capital_per_trade,
        requested_by_user_id=user_id,
    )
    session.add(bot)
    await session.flush()
    return bot


async def approve_options_bot(
    bot: OptionsBot, *, approved_by_user_id: uuid.UUID | None
) -> OptionsBot:
    if bot.status is not OptionsBotStatus.PENDING_APPROVAL:
        raise OptionsBotError(
            f"NOT_PENDING_APPROVAL: bot is {bot.status.value}; only a pending bot can be approved."
        )
    bot.status = OptionsBotStatus.ACTIVE
    bot.approved_by_user_id = approved_by_user_id
    bot.approved_at = datetime.now(UTC)
    _touch(bot)
    return bot


async def pause_options_bot(bot: OptionsBot, *, reason: str | None) -> OptionsBot:
    if bot.status is not OptionsBotStatus.ACTIVE:
        raise OptionsBotError(
            f"NOT_ACTIVE: bot is {bot.status.value}; only an active bot can be paused."
        )
    bot.status = OptionsBotStatus.PAUSED
    bot.paused_reason = reason
    _touch(bot)
    return bot


async def resume_options_bot(bot: OptionsBot) -> OptionsBot:
    if bot.status is not OptionsBotStatus.PAUSED:
        raise OptionsBotError(
            f"NOT_PAUSED: bot is {bot.status.value}; only a paused bot can resume."
        )
    bot.status = OptionsBotStatus.ACTIVE
    bot.paused_reason = None
    _touch(bot)
    return bot


async def stop_options_bot(bot: OptionsBot) -> OptionsBot:
    """Terminal. Open structures are NOT closed here - stopping the robot
    and liquidating what it holds are different decisions (D087). They stay
    on the paper book, can be closed by hand, and settle at expiry."""
    if bot.status is OptionsBotStatus.STOPPED:
        raise OptionsBotError("ALREADY_STOPPED: bot is already stopped.")
    bot.status = OptionsBotStatus.STOPPED
    bot.stopped_at = datetime.now(UTC)
    _touch(bot)
    return bot
