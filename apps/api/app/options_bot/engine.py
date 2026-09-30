"""One cycle of one options paper bot (Phase 102, D122).

Per ACTIVE bot, per cycle, inside a transaction the caller owns:

  1. **Refuse early** - not active, emergency stop, live broker, no chain
     provider. Each writes its own run status and touches nothing else.
  2. **Settle** this bot's structures whose expiry session is over, at
     intrinsic from the underlying's stored daily close (never a guessed
     price; no close means the structure stays open and the run says so).
     Settlement is not an order, so it runs even when the market is closed.
  3. **Market gate** - entries and exits happen in the regular US session on
     a weekday only; outside it the run is `skipped_market_closed`.
  4. **Manage** every open structure: mark it at what closing would fill at
     under the paper haircut (not the mid - a target reached only at a mid
     nobody can trade at is not reached), track the best and worst P&L %,
     and close at the profit target or the stop, through the same close
     path a manual order takes.
  5. **Enter** at most ONE new structure per session day, and only while
     fewer than `max_concurrent_positions` are open: nearest expiry in the
     DTE band, strikes by vendor delta, liquidity-filtered, sized to
     `capital_per_trade` of max loss, then through `submit_open` - i.e. the
     Risk Engine. A per-trade-risk or position-size refusal that names a
     smaller quantity that fits is retried ONCE at that quantity (the same
     single deterministic resize the autotrade bot makes); the refused
     order is still recorded.

What it never does: trade a live broker, price from a model, compute its
own Greeks, fill a leg with no two-sided quote, or liquidate on stop /
pause (D087).

The one-entry-per-day rule is a guard, not a strategy: without it a bot
with room for five positions would open five identical structures in its
first five minutes.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.core.config import Settings
from apps.api.app.db.models import (
    Broker,
    BrokerKind,
    OptionsBot,
    OptionsBotRun,
    OptionsBotRunStatus,
    OptionsBotStatus,
    OptionsBotTrade,
    OptionStructure,
    OptionStructureStatus,
)
from apps.api.app.marketdata.option_chain_provider import OptionChain, OptionChainProvider
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.sessions import SessionPhase, session_phase, to_exchange_time
from apps.api.app.options.paper_book import (
    OpenOrderRequest,
    fetch_chain,
    new_york_date,
    settle_expired,
    submit_close,
    submit_open,
)
from apps.api.app.options.paper_orders import (
    MULTIPLIER,
    OptionOrderError,
    OptionStructureType,
    price_close,
    price_open,
    realized_pnl,
)
from apps.api.app.options_bot.selection import SelectionRefused, build_candidate, pick_expiry
from apps.api.app.risk.models import BlockReason
from apps.api.app.safety.emergency_stop import is_emergency_stop_active

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)


@dataclass
class OptionsBotCycleOutcome:
    bot_id: uuid.UUID
    run_id: uuid.UUID
    status: OptionsBotRunStatus
    detail: str | None = None
    positions_checked: int = 0
    trades_opened: int = 0
    trades_closed: int = 0
    notes: list[str] = field(default_factory=list)


def market_open(ts: datetime) -> bool:
    return to_exchange_time(ts).weekday() < 5 and session_phase(ts) is SessionPhase.REGULAR


def _finish(
    run: OptionsBotRun,
    outcome: OptionsBotCycleOutcome,
    status: OptionsBotRunStatus,
    detail: str | None,
    clock: Callable[[], datetime],
) -> OptionsBotCycleOutcome:
    run.status = status
    run.detail = detail
    run.completed_at = clock()
    run.positions_checked = outcome.positions_checked
    run.trades_opened = outcome.trades_opened
    run.trades_closed = outcome.trades_closed
    outcome.status = status
    outcome.detail = detail
    return outcome


def pnl_pct(*, pnl: Decimal, entry_net_price: Decimal, quantity: int) -> Decimal | None:
    """P&L as a % of the entry premium (debit paid or credit received)."""
    basis = abs(entry_net_price) * MULTIPLIER * quantity
    if basis <= 0:
        return None
    return (pnl / basis * _HUNDRED).quantize(Decimal("0.0001"))


async def _open_trades(session: AsyncSession, bot_id: uuid.UUID) -> list[OptionsBotTrade]:
    return list(
        (
            await session.execute(
                select(OptionsBotTrade).where(
                    OptionsBotTrade.bot_id == bot_id, OptionsBotTrade.closed_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )


async def sync_closed_trades(
    session: AsyncSession, bot: OptionsBot, run: OptionsBotRun
) -> int:
    """Copy the outcome of any structure that is no longer open (closed by
    this bot, by hand on the desk, or settled at expiry) onto the bot's own
    ledger row."""
    closed = 0
    for trade in await _open_trades(session, bot.id):
        structure = await session.get(OptionStructure, trade.structure_id)
        if structure is None or structure.status is OptionStructureStatus.OPEN:
            continue
        trade.closed_at = structure.closed_at
        trade.close_run_id = run.id
        trade.exit_net_price = structure.exit_net_price
        trade.exit_reason = structure.close_reason
        trade.realized_pnl = structure.realized_pnl
        trade.return_on_risk = (
            (structure.realized_pnl / trade.max_loss).quantize(Decimal("0.000001"))
            if structure.realized_pnl is not None and trade.max_loss > 0
            else None
        )
        closed += 1
    return closed


async def _chain_for(
    cache: dict[tuple[str, Any], OptionChain | str],
    provider: OptionChainProvider,
    underlying: str,
    expiry,
) -> OptionChain | str:
    key = (underlying, expiry)
    if key not in cache:
        try:
            cache[key] = await fetch_chain(provider, underlying, expiry)
        except OptionOrderError as exc:
            cache[key] = str(exc)
    return cache[key]


async def _manage(
    session: AsyncSession,
    bot: OptionsBot,
    run: OptionsBotRun,
    broker: Broker,
    *,
    provider: OptionChainProvider,
    settings: Settings,
    now: datetime,
    outcome: OptionsBotCycleOutcome,
    chains: dict[tuple[str, Any], OptionChain | str],
) -> None:
    for trade in await _open_trades(session, bot.id):
        structure = await session.get(OptionStructure, trade.structure_id)
        if structure is None or structure.status is not OptionStructureStatus.OPEN:
            continue
        if structure.expiry < new_york_date(now):
            outcome.notes.append(
                f"{structure.id}: past expiry, awaiting settlement (no stored close yet)"
            )
            continue
        outcome.positions_checked += 1
        chain = await _chain_for(chains, provider, structure.underlying, structure.expiry)
        if isinstance(chain, str):
            outcome.notes.append(f"{structure.id}: not marked - {chain}")
            continue
        try:
            _, exit_value = price_close(
                structure.legs, chain, k=settings.options_paper_fill_haircut_k
            )
        except OptionOrderError as exc:
            outcome.notes.append(f"{structure.id}: not marked - {exc}")
            continue
        pnl = realized_pnl(
            entry_net_price=structure.entry_net_price, exit_value=exit_value,
            quantity=structure.quantity,
        )
        pct = pnl_pct(pnl=pnl, entry_net_price=structure.entry_net_price,
                      quantity=structure.quantity)
        if pct is None:
            continue
        trade.peak_pnl_pct = pct if trade.peak_pnl_pct is None else max(trade.peak_pnl_pct, pct)
        trade.trough_pnl_pct = (
            pct if trade.trough_pnl_pct is None else min(trade.trough_pnl_pct, pct)
        )
        reason = None
        if pct >= trade.profit_target_pct:
            reason = "take_profit"
        elif pct <= -trade.stop_pct:
            reason = "stop_loss"
        if reason is None:
            continue
        try:
            result = await submit_close(
                session, broker=broker, structure_id=structure.id, provider=provider,
                settings=settings, now=now, submitted_by_user_id=None, reason=reason,
                options_bot_run_id=run.id, chain=chain,
            )
        except OptionOrderError as exc:
            outcome.notes.append(f"{structure.id}: {reason} exit not placed - {exc}")
            continue
        if not result.filled:
            outcome.notes.append(
                f"{structure.id}: {reason} exit refused - {result.order.block_reason}: "
                f"{result.order.detail}"
            )


async def _enter(
    session: AsyncSession,
    bot: OptionsBot,
    run: OptionsBotRun,
    broker: Broker,
    *,
    provider: OptionChainProvider,
    settings: Settings,
    now: datetime,
    outcome: OptionsBotCycleOutcome,
    chains: dict[tuple[str, Any], OptionChain | str],
) -> None:
    open_trades = await _open_trades(session, bot.id)
    if len(open_trades) >= bot.max_concurrent_positions:
        outcome.notes.append(
            f"no entry: {len(open_trades)}/{bot.max_concurrent_positions} positions open"
        )
        return
    today = new_york_date(now)
    opened_today = (
        await session.execute(
            select(OptionsBotTrade.opened_at).where(OptionsBotTrade.bot_id == bot.id)
        )
    ).scalars().all()
    if any(new_york_date(ts) == today for ts in opened_today):
        outcome.notes.append("no entry: one new structure per session day, already opened today")
        return

    structure = OptionStructureType(bot.structure_type)
    try:
        expiries = await provider.get_expiries(bot.underlying)
    except (DataUnavailableError, VendorError) as exc:
        outcome.notes.append(f"no entry: expiries unavailable - {exc}")
        return
    try:
        expiry = pick_expiry(expiries, today=today, dte_min=bot.dte_min, dte_max=bot.dte_max)
    except SelectionRefused as exc:
        outcome.notes.append(f"no entry: {exc}")
        return
    chain = await _chain_for(chains, provider, bot.underlying, expiry)
    if isinstance(chain, str):
        outcome.notes.append(f"no entry: {chain}")
        return
    try:
        candidate = build_candidate(
            structure, chain, target_delta=bot.target_delta, spread_width=bot.spread_width
        )
        econ = price_open(
            structure, candidate.legs, chain, k=settings.options_paper_fill_haircut_k
        )
    except (SelectionRefused, OptionOrderError) as exc:
        outcome.notes.append(f"no entry: {exc}")
        return

    quantity = int(
        (bot.capital_per_trade / econ.max_loss_per_contract).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    if quantity < 1:
        outcome.notes.append(
            f"no entry: capital per trade {bot.capital_per_trade} is below one structure's "
            f"max loss {econ.max_loss_per_contract}"
        )
        return

    async def _submit(qty: int):
        return await submit_open(
            session, broker=broker,
            request=OpenOrderRequest(
                underlying=bot.underlying, expiry=expiry, structure_type=structure,
                legs=candidate.legs, quantity=qty,
            ),
            provider=provider, settings=settings, now=now, submitted_by_user_id=None,
            options_bot_id=bot.id, options_bot_run_id=run.id, chain=chain,
        )

    try:
        result = await _submit(quantity)
        fits = result.decision.max_quantity_allowed
        if (
            not result.filled
            and result.decision.reason
            in (BlockReason.EXCEEDS_PER_TRADE_RISK, BlockReason.EXCEEDS_MAX_POSITION_SIZE)
            and fits is not None
            and 1 <= fits < quantity
        ):
            outcome.notes.append(
                f"entry resized {quantity} -> {int(fits)} contracts: {result.decision.reason.value}"
            )
            result = await _submit(int(fits))
    except OptionOrderError as exc:
        outcome.notes.append(f"no entry: {exc}")
        return
    if not result.filled or result.structure is None:
        outcome.notes.append(
            f"entry refused by the Risk Engine: {result.order.block_reason} - {result.order.detail}"
        )
        return
    s = result.structure
    session.add(
        OptionsBotTrade(
            id=uuid.uuid4(),
            bot_id=bot.id,
            structure_id=s.id,
            open_run_id=run.id,
            structure_type=s.structure_type,
            underlying=s.underlying,
            expiry=s.expiry,
            quantity=s.quantity,
            target_delta=bot.target_delta,
            entry_delta=candidate.anchor_delta,
            entry_net_price=s.entry_net_price,
            max_loss=s.max_loss,
            profit_target_pct=bot.profit_target_pct,
            stop_pct=bot.stop_pct,
            opened_at=now,
        )
    )
    outcome.trades_opened += 1
    outcome.notes.append(
        f"opened {s.quantity} x {s.structure_type} {s.underlying} {s.expiry.isoformat()} "
        f"at net {s.entry_net_price} (max loss {s.max_loss}; {chain.source} as of "
        f"{chain.as_of.isoformat()})"
    )


async def run_options_bot_cycle(
    session: AsyncSession,
    bot: OptionsBot,
    run: OptionsBotRun,
    *,
    settings: Settings,
    clock: Callable[[], datetime],
    provider: OptionChainProvider | None,
    bar_router: Any | None = None,
) -> OptionsBotCycleOutcome:
    """The work for one bot. `run` exists and is flushed; this resolves it."""
    outcome = OptionsBotCycleOutcome(
        bot_id=bot.id, run_id=run.id, status=OptionsBotRunStatus.FAILED
    )
    now = clock()

    if bot.status is not OptionsBotStatus.ACTIVE:
        return _finish(run, outcome, OptionsBotRunStatus.SKIPPED_NOT_ACTIVE,
                       f"bot is {bot.status.value}", clock)
    if await is_emergency_stop_active(session, settings_default=settings.emergency_stop_active):
        return _finish(run, outcome, OptionsBotRunStatus.SKIPPED_EMERGENCY_STOP,
                       "global emergency stop is active", clock)
    broker = await session.get(Broker, bot.broker_id)
    if broker is None or broker.kind is not BrokerKind.PAPER:
        return _finish(
            run, outcome, OptionsBotRunStatus.SKIPPED_LIVE_NOT_SUPPORTED,
            "option routing is PAPER ONLY; a live broker is refused before any quote is read "
            "or any order path touched",
            clock,
        )
    if provider is None:
        return _finish(run, outcome, OptionsBotRunStatus.SKIPPED_NOT_CONFIGURED,
                       "NOT_CONFIGURED: no option chain provider is wired", clock)

    settled = await settle_expired(
        session, broker_id=broker.id, settings=settings, now=now, bar_router=bar_router,
        only_bot_id=bot.id,
    )
    for r in settled:
        outcome.notes.append(f"{r.structure.id}: {r.detail}")
    outcome.trades_closed += await sync_closed_trades(session, bot, run)

    if not market_open(now):
        phase = session_phase(now)
        closed_detail = "; ".join(
            [f"market is {phase.value if phase else 'closed'}; bot trades the regular session",
             *outcome.notes]
        )
        return _finish(
            run, outcome, OptionsBotRunStatus.SKIPPED_MARKET_CLOSED, closed_detail, clock
        )

    chains: dict[tuple[str, Any], OptionChain | str] = {}
    await _manage(session, bot, run, broker, provider=provider, settings=settings, now=now,
                  outcome=outcome, chains=chains)
    outcome.trades_closed += await sync_closed_trades(session, bot, run)
    await _enter(session, bot, run, broker, provider=provider, settings=settings, now=now,
                 outcome=outcome, chains=chains)

    bot.last_evaluated_at = clock()
    detail = "; ".join(outcome.notes) if outcome.notes else None
    return _finish(run, outcome, OptionsBotRunStatus.SUCCEEDED, detail, clock)
