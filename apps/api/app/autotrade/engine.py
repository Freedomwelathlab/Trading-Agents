"""One cycle of one Autotrade Bot (Phase 81, D098).

Per ACTIVE bot, per cycle, inside a transaction the caller owns:

  1. **Refuse early** — not active, emergency stop, live broker, no
     market-data vendor, or the market phase the bot trades is closed.
     Each writes its own run status and touches nothing else.
  2. **Refresh bars** for every symbol from the vendor into the persisted
     store (`market_data_bars`), so the scan reads what the vendor
     returned minutes ago rather than whatever the last manual backfill
     left behind. Nothing is fabricated: a symbol the vendor cannot serve
     keeps its stale bars and the scan reports "no fresh bar".
  3. **Manage open positions first** — stop / trailing stop / take profit /
     trailing take profit / session-end flat, through
     `autotrade/brackets.py`. Exits go through the ONE sanctioned
     RISK -> PORTFOLIO -> BROKER path exactly as entries do.
  4. **Count what is still allowed**: open positions against
     `max_trades_per_session`, positions opened today against
     `max_trades_per_day`.
  5. **Scan** every symbol the bot is not already in, rank the hits by
     score, and open the best `k`, sized to `capital_per_trade`.
  6. **Write the run row** with what happened and, if nothing did, why.

What this reuses unchanged: the intraday detectors (`setups.py`), the
bracket sizing/quality helpers, `MarketDataStore`, the paper broker
persistence, and `submit_trade_and_record` — the bot invents no private
order path. What it does not do: short (paper broker cannot), trade a
live broker (refused with its own status; D087's live path is a separate,
separately-gated build), or hold overnight.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.routes.strategy_backtests import _portfolio_limits, _risk_limits
from apps.api.app.autotrade.brackets import (
    BracketState,
    ExitRules,
    initial_bracket,
    manage,
)
from apps.api.app.autotrade.learning import (
    active_setups,
    active_setups_for_symbol,
    closed_trades,
    stats_by_setup,
    stats_by_setup_symbol,
    write_pending_insights,
)
from apps.api.app.autotrade.scanner import ScanHit, scan_latest_bar
from apps.api.app.backtesting.brackets import BracketPlan
from apps.api.app.core.config import Settings
from apps.api.app.core.logging import get_logger
from apps.api.app.db.models import (
    AutotradeBot,
    AutotradeBotRun,
    AutotradeBotRunStatus,
    AutotradeBotStatus,
    AutotradeBotTrade,
    AutotradeExitReason,
    Broker,
    BrokerKind,
    Order,
)
from apps.api.app.execution.paper_broker import PaperBrokerAdapter
from apps.api.app.execution.persistence import load_paper_broker, save_paper_broker
from apps.api.app.marketdata.bar_router import BarBackfillRouter, UnroutableSymbolError
from apps.api.app.marketdata.news_provider import NewsProvider
from apps.api.app.marketdata.portfolio_risk import load_market_risk_inputs
from apps.api.app.marketdata.sessions import session_date, session_phase
from apps.api.app.marketdata.store import MarketDataStore
from apps.api.app.oms.persistence import get_recent_filled_orders, submit_trade_and_record
from apps.api.app.oms.service import OMSStatus
from apps.api.app.portfolio_manager.manager import portfolio_state_from_positions
from apps.api.app.risk.models import BlockReason, Side, TradeProposal
from apps.api.app.safety.emergency_stop import is_emergency_stop_active

logger = get_logger(__name__)

BARS_TO_LOAD = 700
"""Enough 5-minute bars for the current extended session plus the previous
one (192 each) with room for gaps — levels need the previous session's
extremes and swings need the current one's history."""

REFRESH_LOOKBACK_DAYS = 5
"""Calendar days asked of the vendor on every refresh. Covers a long
weekend plus a holiday; the upsert is idempotent so re-fetching the
overlap costs nothing but the call."""

_INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


@dataclass
class BotCycleOutcome:
    bot_id: uuid.UUID
    run_id: uuid.UUID
    status: AutotradeBotRunStatus
    detail: str | None = None
    symbols_scanned: int = 0
    signals_found: int = 0
    signals_skipped: int = 0
    trades_opened: int = 0
    trades_closed: int = 0
    setups_active: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _rules(bot: AutotradeBot) -> ExitRules:
    return ExitRules(
        stop_loss_mode=bot.stop_loss_mode,
        stop_loss_max_pct=bot.stop_loss_max_pct,
        trailing_stop_pct=bot.trailing_stop_pct,
        take_profit_mode=bot.take_profit_mode,
        take_profit_min_pct=bot.take_profit_min_pct,
        trailing_take_profit_pct=bot.trailing_take_profit_pct,
    )


def phase_allowed(ts: datetime, market_type: str) -> bool:
    phase = session_phase(ts)
    if phase is None:
        return False
    if market_type == "auto":
        return True
    return phase.value == {"pre_market": "pre_market", "post_market": "after_hours"}.get(
        market_type, "regular"
    )


def session_ending(last_bar_ts: datetime, *, market_type: str, bar_interval: str) -> bool:
    """True when the bar AFTER this one would fall outside the bot's
    tradeable phase — i.e. this is the last bar the bot may still act on."""
    minutes = _INTERVAL_MINUTES.get(bar_interval, 5)
    return not phase_allowed(last_bar_ts + timedelta(minutes=minutes), market_type)


def _finish(
    run: AutotradeBotRun,
    outcome: BotCycleOutcome,
    status: AutotradeBotRunStatus,
    detail: str | None,
    clock: Callable[[], datetime],
) -> BotCycleOutcome:
    run.status = status
    run.detail = detail
    run.completed_at = clock()
    run.symbols_scanned = outcome.symbols_scanned
    run.signals_found = outcome.signals_found
    run.signals_skipped = outcome.signals_skipped
    run.trades_opened = outcome.trades_opened
    run.trades_closed = outcome.trades_closed
    run.setups_active = list(outcome.setups_active)
    outcome.status = status
    outcome.detail = detail
    return outcome


async def _refresh_bars(
    store: MarketDataStore,
    router: BarBackfillRouter,
    symbols: Sequence[str],
    *,
    bar_interval: str,
    as_of: datetime,
    notes: list[str],
) -> None:
    end = session_date(as_of)
    start = end - timedelta(days=REFRESH_LOOKBACK_DAYS)
    for symbol in symbols:
        try:
            fresh = await router.get_bars(
                symbol, bar_interval=bar_interval, start_date=start, end_date=end
            )
        except UnroutableSymbolError as exc:
            notes.append(f"{symbol}: unroutable ({exc})")
            continue
        except Exception as exc:  # noqa: BLE001 - a vendor failure for ONE symbol must not kill the cycle
            notes.append(f"{symbol}: vendor error {type(exc).__name__}: {exc}")
            continue
        if fresh:
            await store.upsert_bars(fresh)


def _latest(bars: Sequence) -> tuple[Decimal, datetime] | None:
    if not bars:
        return None
    last = bars[-1]
    return last.close, last.ts


async def _manage_open_trades(
    session: AsyncSession,
    bot: AutotradeBot,
    run: AutotradeBotRun,
    *,
    broker: PaperBrokerAdapter,
    bars_by_symbol: dict[str, list],
    marks: dict[str, Decimal],
    settings: Settings,
    outcome: BotCycleOutcome,
) -> None:
    open_trades = (
        await session.execute(
            select(AutotradeBotTrade).where(
                AutotradeBotTrade.bot_id == bot.id, AutotradeBotTrade.closed_at.is_(None)
            )
        )
    ).scalars().all()
    rules = _rules(bot)
    risk_limits = _risk_limits(settings)
    portfolio_limits = _portfolio_limits(settings)

    for trade in open_trades:
        bars = bars_by_symbol.get(trade.symbol) or []
        if not bars:
            outcome.notes.append(f"{trade.symbol}: open position has no bars to manage against")
            continue
        last = bars[-1]
        high = last.high if last.high is not None else last.close
        low = last.low if last.low is not None else last.close
        decision = manage(
            BracketState(
                entry_price=trade.entry_price,
                initial_stop_price=trade.initial_stop_price,
                stop_price=trade.stop_price,
                take_profit_price=trade.take_profit_price,
                peak_price=trade.peak_price,
                take_profit_armed=trade.take_profit_armed,
            ),
            bar_high=high,
            bar_low=low,
            bar_close=last.close,
            rules=rules,
            session_ending=session_ending(
                last.ts, market_type=bot.market_type, bar_interval=bot.bar_interval
            ),
        )
        trade.stop_price = decision.state.stop_price
        trade.peak_price = decision.state.peak_price
        trade.trough_price = min(trade.trough_price, low)
        trade.take_profit_armed = decision.state.take_profit_armed
        if not decision.should_exit:
            continue

        held = broker.positions.get(trade.symbol, Decimal(0))
        quantity = min(trade.quantity, held)
        if quantity <= 0:
            # The broker no longer holds it (closed by hand on the desk).
            # Record the bot's own ledger as closed by the operator at the
            # last known price rather than leaving a phantom open row.
            trade.closed_at = last.ts
            trade.close_run_id = run.id
            trade.exit_price = last.close
            trade.exit_reason = AutotradeExitReason.OPERATOR.value
            _settle(trade)
            outcome.trades_closed += 1
            outcome.notes.append(f"{trade.symbol}: position already flat at broker; ledger closed")
            continue

        proposal = TradeProposal(
            symbol=trade.symbol,
            side=Side.SELL,
            quantity=quantity,
            estimated_price=last.close,
            stop_price=None,
            market_data_as_of=last.ts,
        )
        result = await _submit(
            session, bot, broker, proposal, marks=marks, settings=settings,
            risk_limits=risk_limits, portfolio_limits=portfolio_limits, now=last.ts,
        )
        if result.order_id is not None:
            order = await session.get(Order, result.order_id)
            if order is not None:
                order.autotrade_bot_run_id = run.id
        if result.status is not OMSStatus.FILLED or result.fill is None:
            outcome.notes.append(
                f"{trade.symbol}: exit "
                f"({decision.exit_reason.value if decision.exit_reason else '?'}) "
                f"not filled: {result.status.value} {result.risk_decision.reason}"
            )
            continue
        trade.closed_at = result.fill.filled_at
        trade.close_run_id = run.id
        trade.exit_order_id = result.order_id
        trade.exit_price = result.fill.fill_price
        trade.exit_reason = decision.exit_reason.value if decision.exit_reason else None
        _settle(trade)
        marks[trade.symbol] = result.fill.fill_price
        outcome.trades_closed += 1


def _settle(trade: AutotradeBotTrade) -> None:
    if trade.exit_price is None:
        return
    trade.realized_pnl = (trade.exit_price - trade.entry_price) * trade.quantity
    risk = trade.entry_price - trade.initial_stop_price
    trade.r_multiple = (
        (trade.exit_price - trade.entry_price) / risk if risk > 0 else None
    )


async def _submit(
    session: AsyncSession,
    bot: AutotradeBot,
    broker: PaperBrokerAdapter,
    proposal: TradeProposal,
    *,
    marks: dict[str, Decimal],
    settings: Settings,
    risk_limits,
    portfolio_limits,
    now: datetime,
):
    """The one order path, with the runner's single deterministic resize
    retry when the ONLY objection was position size."""
    store = MarketDataStore(session)
    account = broker.get_account_state(marks=marks)
    portfolio = portfolio_state_from_positions(broker.positions, marks, broker.cash)
    recent_orders = await get_recent_filled_orders(
        session, bot.broker_id, proposal.symbol,
        window_seconds=settings.risk_duplicate_order_window_seconds,
    )
    market_risk = await load_market_risk_inputs(
        store,
        [proposal.symbol, *portfolio.open_symbols],
        as_of=now.date(),
        lookback_days=settings.portfolio_market_risk_lookback_days,
        min_observations=settings.portfolio_market_risk_min_observations,
    )
    emergency = await is_emergency_stop_active(
        session, settings_default=settings.emergency_stop_active
    )
    kwargs = dict(
        emergency_stop_active=emergency,
        now=now,
        submitted_by_user_id=None,
        recent_orders=recent_orders,
        portfolio=portfolio,
        portfolio_limits=portfolio_limits,
        market_risk=market_risk,
    )
    result = await submit_trade_and_record(
        session, bot.broker_id, proposal, account, risk_limits, broker,
        **kwargs,  # type: ignore[arg-type]
    )
    if (
        result.status is OMSStatus.REJECTED
        and result.risk_decision.reason is BlockReason.EXCEEDS_MAX_POSITION_SIZE
        and result.risk_decision.max_quantity_allowed
        and result.portfolio_decision is None
        and 0 < result.risk_decision.max_quantity_allowed < proposal.quantity
    ):
        retry = proposal.model_copy(
            update={"quantity": result.risk_decision.max_quantity_allowed}
        )
        result = await submit_trade_and_record(
            session, bot.broker_id, retry, account, risk_limits, broker,
            **kwargs,  # type: ignore[arg-type]
        )
    return result


async def _recent_headline(
    news: NewsProvider | None, symbol: str, *, within_minutes: int, now: datetime
) -> str | None:
    if news is None or within_minutes <= 0:
        return None
    try:
        headlines = await news.get_recent_headlines(symbol, limit=5)
    except Exception as exc:  # noqa: BLE001 - news is a filter, not a dependency
        return f"news check failed ({type(exc).__name__}); entry skipped to fail closed"
    cutoff = now - timedelta(minutes=within_minutes)
    for h in headlines:
        if h.published_at >= cutoff:
            return f"headline {h.published_at.isoformat()}: {h.title[:80]}"
    return None


async def run_bot_cycle(
    session: AsyncSession,
    bot: AutotradeBot,
    run: AutotradeBotRun,
    *,
    settings: Settings,
    clock: Callable[[], datetime],
    bar_router: BarBackfillRouter | None,
    news_provider: NewsProvider | None = None,
    plan: BracketPlan | None = None,
) -> BotCycleOutcome:
    """The work for one bot. `run` exists and is flushed; this resolves it."""
    outcome = BotCycleOutcome(bot_id=bot.id, run_id=run.id, status=AutotradeBotRunStatus.FAILED)
    plan = plan or BracketPlan()
    now = clock()

    if bot.status is not AutotradeBotStatus.ACTIVE:
        return _finish(run, outcome, AutotradeBotRunStatus.SKIPPED_NOT_ACTIVE,
                       f"bot is {bot.status.value}", clock)

    if await is_emergency_stop_active(session, settings_default=settings.emergency_stop_active):
        return _finish(run, outcome, AutotradeBotRunStatus.SKIPPED_EMERGENCY_STOP,
                       "global emergency stop is active", clock)

    broker_row = await session.get(Broker, bot.broker_id)
    if broker_row is None or broker_row.kind is BrokerKind.LIVE:
        return _finish(
            run, outcome, AutotradeBotRunStatus.SKIPPED_LIVE_NOT_SUPPORTED,
            "the bot runs on paper brokers only in this version; a live broker is refused "
            "before any market data or order path is touched",
            clock,
        )

    if bar_router is None or "longbridge" not in bar_router.configured_vendors:
        return _finish(
            run, outcome, AutotradeBotRunStatus.SKIPPED_NOT_CONFIGURED,
            "NOT_CONFIGURED: no equity market-data vendor is wired (LONGPORT_* unset)", clock,
        )

    if not phase_allowed(now, bot.market_type):
        phase = session_phase(now)
        return _finish(
            run, outcome, AutotradeBotRunStatus.SKIPPED_MARKET_CLOSED,
            f"market phase is {phase.value if phase else 'closed'}; bot trades {bot.market_type}",
            clock,
        )

    store = MarketDataStore(session)
    paper = await load_paper_broker(
        session, bot.broker_id, default_starting_cash=settings.paper_broker_starting_cash
    )
    # Refresh the bot's own symbols AND anything else the broker holds (a
    # position opened by hand on the same broker): the risk engine values
    # the whole account before it approves an order, and the honest
    # alternative to fetching a held symbol's bar is refusing the cycle.
    held_extra = [s for s in paper.positions if s not in bot.symbols]
    await _refresh_bars(
        store, bar_router, [*bot.symbols, *held_extra], bar_interval=bot.bar_interval,
        as_of=now, notes=outcome.notes,
    )

    bars_by_symbol: dict[str, list] = {}
    for symbol in bot.symbols:
        bars_by_symbol[symbol] = await store.get_latest_bars(
            symbol, bar_interval=bot.bar_interval, count=BARS_TO_LOAD
        )

    # Marks for everything the broker holds (bot positions and anything
    # placed by hand on the same broker). A held symbol with no bar cannot
    # be valued and no price is fabricated — the cycle fails, visibly.
    marks: dict[str, Decimal] = {}
    for held_symbol in paper.positions:
        bars = bars_by_symbol.get(held_symbol)
        if bars is None:
            bars = await store.get_latest_bars(held_symbol, bar_interval=bot.bar_interval, count=1)
        priced = _latest(bars)
        if priced is None:
            return _finish(
                run, outcome, AutotradeBotRunStatus.FAILED,
                f"held position {held_symbol} has no {bot.bar_interval} bar to mark against; "
                f"no price was fabricated", clock,
            )
        marks[held_symbol] = priced[0]

    # 3. Manage what is open before looking for anything new.
    await _manage_open_trades(
        session, bot, run, broker=paper, bars_by_symbol=bars_by_symbol, marks=marks,
        settings=settings, outcome=outcome,
    )

    # 4. Remaining allowance.
    open_rows = (
        await session.execute(
            select(AutotradeBotTrade).where(
                AutotradeBotTrade.bot_id == bot.id, AutotradeBotTrade.closed_at.is_(None)
            )
        )
    ).scalars().all()
    open_symbols = {t.symbol for t in open_rows}
    today = session_date(now)
    opened_today = (
        await session.execute(
            select(AutotradeBotTrade).where(
                AutotradeBotTrade.bot_id == bot.id, AutotradeBotTrade.session_date == today
            )
        )
    ).scalars().all()
    allowance = min(
        bot.max_trades_per_session - len(open_rows),
        bot.max_trades_per_day - len(opened_today),
    )

    # 5. Scan.
    history = await closed_trades(session, bot.id)
    by_setup = stats_by_setup(history)
    by_pair = stats_by_setup_symbol(history)
    setups = active_setups(list(bot.setups), strategy_mode=bot.strategy_mode, stats=by_setup)
    outcome.setups_active = setups

    hits: list[ScanHit] = []
    for symbol in bot.symbols:
        if symbol in open_symbols:
            continue
        outcome.symbols_scanned += 1
        # Phase 83: the per-symbol refinement — a setup demoted on THIS
        # ticker's own evidence is skipped here even if fine elsewhere.
        symbol_setups = active_setups_for_symbol(
            list(bot.setups), strategy_mode=bot.strategy_mode, symbol=symbol,
            by_setup=by_setup, by_pair=by_pair,
        )
        scan = scan_latest_bar(
            symbol, bars_by_symbol.get(symbol) or [], setups=symbol_setups,
            market_type=bot.market_type, min_score=bot.min_score, plan=plan,
        )
        if scan.hit is None:
            if scan.reason.startswith("rejected:"):
                outcome.signals_skipped += 1
                outcome.notes.append(f"{symbol}: {scan.reason}")
            continue
        outcome.signals_found += 1
        hits.append(scan.hit)

    if not setups:
        outcome.notes.append("no setups active after the learning loop's demotions")

    hits.sort(key=lambda h: (-h.signal.score, bot.symbols.index(h.symbol)))
    if allowance <= 0 and hits:
        outcome.notes.append(
            f"{len(hits)} signal(s) not taken: allowance exhausted "
            f"(open {len(open_rows)}/{bot.max_trades_per_session}, "
            f"today {len(opened_today)}/{bot.max_trades_per_day})"
        )

    # 6. Open the best ones.
    risk_limits = _risk_limits(settings)
    portfolio_limits = _portfolio_limits(settings)
    rules = _rules(bot)
    for hit in hits:
        if allowance <= 0:
            break
        blackout = await _recent_headline(
            news_provider, hit.symbol, within_minutes=bot.news_blackout_minutes, now=now
        )
        if blackout:
            outcome.signals_skipped += 1
            outcome.notes.append(f"{hit.symbol}: news blackout — {blackout}")
            continue

        price = hit.last_close
        quantity = (bot.capital_per_trade / price).to_integral_value(rounding=ROUND_FLOOR)
        if quantity <= 0:
            outcome.signals_skipped += 1
            outcome.notes.append(
                f"{hit.symbol}: capital per trade {bot.capital_per_trade} buys 0 shares at {price}"
            )
            continue
        bracket = initial_bracket(
            entry_price=price, structural_stop=hit.signal.stop_price, rules=rules
        )
        if bracket.stop_price >= price:
            outcome.signals_skipped += 1
            outcome.notes.append(f"{hit.symbol}: no valid stop below entry; not traded")
            continue

        marks[hit.symbol] = price
        proposal = TradeProposal(
            symbol=hit.symbol,
            side=Side.BUY,
            quantity=quantity,
            estimated_price=price,
            stop_price=bracket.stop_price,
            market_data_as_of=hit.last_ts,  # type: ignore[arg-type]
        )
        result = await _submit(
            session, bot, paper, proposal, marks=marks, settings=settings,
            risk_limits=risk_limits, portfolio_limits=portfolio_limits,
            now=hit.last_ts,  # type: ignore[arg-type]
        )
        if result.order_id is not None:
            order = await session.get(Order, result.order_id)
            if order is not None:
                order.autotrade_bot_run_id = run.id
        if result.status is not OMSStatus.FILLED or result.fill is None:
            outcome.signals_skipped += 1
            outcome.notes.append(
                f"{hit.symbol}: entry not filled: {result.status.value} "
                f"{result.risk_decision.reason}"
            )
            continue

        fill = result.fill
        # Re-anchor the bracket on the actual fill so R is measured from
        # what was paid, not from the bar close the signal was priced at.
        bracket = initial_bracket(
            entry_price=fill.fill_price, structural_stop=hit.signal.stop_price, rules=rules
        )
        session.add(
            AutotradeBotTrade(
                id=uuid.uuid4(),
                bot_id=bot.id,
                open_run_id=run.id,
                symbol=hit.symbol,
                session_date=today,
                setup_name=hit.signal.setup_name,
                score=hit.signal.score,
                evidence=dict(hit.signal.evidence),
                quantity=fill.quantity,
                entry_order_id=result.order_id,
                entry_price=fill.fill_price,
                initial_stop_price=bracket.initial_stop_price,
                stop_price=bracket.stop_price,
                take_profit_price=bracket.take_profit_price,
                peak_price=fill.fill_price,
                trough_price=fill.fill_price,
                take_profit_armed=False,
                opened_at=fill.filled_at,
            )
        )
        outcome.trades_opened += 1
        allowance -= 1

    await save_paper_broker(session, bot.broker_id, paper)

    # Phase 83: the journal. Sessions before today that still have no
    # insight row get one now; today's is written on the cycle that ends
    # the session (every position is flat by then, so the day is final).
    ending_today = any(
        bars_by_symbol.get(s) and session_ending(
            bars_by_symbol[s][-1].ts, market_type=bot.market_type, bar_interval=bot.bar_interval
        )
        for s in bot.symbols
    )
    written = await write_pending_insights(
        session, bot.id, min_score=bot.min_score, before=today,
        include=today if ending_today else None,
    )
    if written:
        outcome.notes.append(
            "insights written for " + ", ".join(w.session_date.isoformat() for w in written)
        )
    bot.last_evaluated_at = clock()
    detail = "; ".join(outcome.notes) if outcome.notes else None
    return _finish(run, outcome, AutotradeBotRunStatus.SUCCEEDED, detail, clock)
