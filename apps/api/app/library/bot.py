"""One cycle of a Strategy Library paper bot (Phase 109, D136).

The bot holds what its strategy says to hold. Each cycle it:

  1. asks the strategy for target weights from COMPLETED daily sessions;
  2. gets a quote for everything it holds or should hold;
  3. values its own sub-account (its cash plus its own positions);
  4. if the strategy has made a new decision - or the bot's holdings do
     not match the target - sells what should go, then buys what should
     come in, each order through the same Risk Engine path as any other;
  5. writes a run row saying what it did and, if nothing, why.

What it will not do: trade a live broker, short (a negative weight is
left as cash, and said so), trade on a quote older than the configured
limit, or chase a rejection - a symbol the Risk Engine refused is left
alone until the strategy decides again.

Timing matches the backtest by construction: the strategy sees only
finished sessions, so a decision made on one close is traded during the
next session, never on the close that produced it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.api.routes.strategy_backtests import _portfolio_limits, _risk_limits
from apps.api.app.autotrade.engine import _submit as submit_through_risk
from apps.api.app.core.config import Settings
from apps.api.app.core.logging import get_logger
from apps.api.app.db.models import (
    Broker,
    BrokerKind,
    LibraryBot,
    LibraryBotRun,
    LibraryBotRunStatus,
    LibraryBotStatus,
)
from apps.api.app.execution.persistence import load_paper_broker, save_paper_broker
from apps.api.app.library.engine import PriceTable, run_strategy
from apps.api.app.library.service import DailyHistoryProvider, LibraryError, load_histories
from apps.api.app.library.strategies import default_cost_bps, get_strategy, market_of
from apps.api.app.marketdata.models import MarketSnapshot
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.oms.service import OMSStatus
from apps.api.app.risk.models import Side, TradeProposal
from apps.api.app.safety.emergency_stop import is_emergency_stop_active

logger = get_logger(__name__)

CRYPTO_STEP = Decimal("0.00000001")
MIN_TRADE_FRACTION = Decimal("0.01")
"""A rebalance smaller than 1% of the bot's equity is not sent."""
MIN_TRADE_NOTIONAL = Decimal("10")
CAP_HEADROOM = Decimal("0.999")
"""Orders are sized a hair under a cap so a rounding digit cannot breach it."""


class QuoteProvider(Protocol):
    async def get_quote(self, symbol: str) -> MarketSnapshot: ...


@dataclass
class OrderLine:
    symbol: str
    side: str
    quantity: str
    price: str
    quote_as_of: str
    quote_source: str
    outcome: str
    """filled | rejected | skipped"""
    detail: str | None = None
    order_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class LibraryBotOutcome:
    bot_id: uuid.UUID
    run_id: uuid.UUID
    status: LibraryBotRunStatus
    detail: str | None = None
    orders_filled: int = 0
    orders_rejected: int = 0
    equity: Decimal | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TargetDecision:
    weights: dict[str, float]
    decided_on: date | None
    explain: dict[str, str]
    as_of: date


def quantize(symbol: str, quantity: Decimal) -> Decimal:
    """Whole units, except crypto which trades in fractions."""
    step = CRYPTO_STEP if market_of(symbol) == "CRYPTO" else Decimal(1)
    return max(quantity, Decimal(0)).quantize(step, rounding=ROUND_FLOOR)


async def decide_target(
    provider: DailyHistoryProvider,
    strategy_id: str,
    symbols: list[str],
    params: dict[str, Any],
    *,
    now: datetime,
) -> TargetDecision:
    """What the strategy holds as of the last COMPLETED session."""
    strategy = get_strategy(strategy_id)
    resolved = strategy.resolve_params(params)
    histories = await load_histories(provider, symbols, now=now)
    table = PriceTable(histories)
    run = await asyncio.to_thread(
        run_strategy,
        table,
        strategy.fn,
        resolved,
        eval_index=len(table) - 1,
        lag=1,
        cost_bps=default_cost_bps,
    )
    return TargetDecision(
        weights=dict(run.target),
        decided_on=run.decided_on,
        explain=dict(run.explain),
        as_of=table.dates[-1],
    )


def _finish(
    run: LibraryBotRun,
    outcome: LibraryBotOutcome,
    status: LibraryBotRunStatus,
    detail: str | None,
    clock: Callable[[], datetime],
) -> LibraryBotOutcome:
    outcome.status = status
    notes = "; ".join(outcome.notes)
    outcome.detail = ". ".join(part for part in (detail, notes) if part) or None
    run.status = status
    run.detail = outcome.detail
    run.completed_at = clock()
    run.orders_filled = outcome.orders_filled
    run.orders_rejected = outcome.orders_rejected
    run.equity = outcome.equity
    return outcome


def _own_positions(bot: LibraryBot) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for symbol, raw in (bot.positions or {}).items():
        quantity = Decimal(str(raw))
        if quantity > 0:
            out[symbol] = quantity
    return out


def bot_equity(cash: Decimal, positions: dict[str, Decimal], prices: dict[str, Decimal]) -> Decimal:
    return cash + sum((q * prices[s] for s, q in positions.items()), Decimal(0))


async def run_library_bot_cycle(
    session: AsyncSession,
    bot: LibraryBot,
    run: LibraryBotRun,
    *,
    settings: Settings,
    clock: Callable[[], datetime],
    provider: DailyHistoryProvider | None,
    quotes: QuoteProvider | None,
) -> LibraryBotOutcome:
    """The work for one bot. `run` exists and is flushed; this resolves it."""
    outcome = LibraryBotOutcome(bot_id=bot.id, run_id=run.id, status=LibraryBotRunStatus.FAILED)
    now = clock()
    bot.last_evaluated_at = now

    if bot.status is not LibraryBotStatus.ACTIVE:
        return _finish(
            run,
            outcome,
            LibraryBotRunStatus.SKIPPED_NOT_ACTIVE,
            f"bot is {bot.status.value}",
            clock,
        )
    if await is_emergency_stop_active(session, settings_default=settings.emergency_stop_active):
        return _finish(
            run,
            outcome,
            LibraryBotRunStatus.SKIPPED_EMERGENCY_STOP,
            "global emergency stop is active",
            clock,
        )
    broker_row = await session.get(Broker, bot.broker_id)
    if broker_row is None or broker_row.kind is not BrokerKind.PAPER:
        return _finish(
            run,
            outcome,
            LibraryBotRunStatus.SKIPPED_LIVE_NOT_SUPPORTED,
            "library bots run on paper brokers only; a live broker is refused before any "
            "market data or order path is touched",
            clock,
        )
    if provider is None or quotes is None:
        return _finish(
            run,
            outcome,
            LibraryBotRunStatus.SKIPPED_NOT_CONFIGURED,
            "NOT_CONFIGURED: the library data source is switched off (LIBRARY_DATA_PROVIDER=none)",
            clock,
        )

    try:
        decision = await decide_target(
            provider, bot.strategy_id, list(bot.symbols), dict(bot.params or {}), now=now
        )
    except (LibraryError, KeyError, ValueError) as exc:
        return _finish(run, outcome, LibraryBotRunStatus.FAILED, str(exc), clock)

    shorts = sorted(s for s, w in decision.weights.items() if w < 0)
    if shorts:
        outcome.notes.append(
            f"short leg left as cash (paper bots are long-only): {', '.join(shorts)}"
        )
    weights = {s: Decimal(str(round(w, 8))) for s, w in decision.weights.items() if w > 0}
    run.decision_date = decision.decided_on

    paper = await load_paper_broker(
        session, bot.broker_id, default_starting_cash=settings.paper_broker_starting_cash
    )
    own = _own_positions(bot)
    for symbol, quantity in list(own.items()):
        at_broker = max(paper.positions.get(symbol, Decimal(0)), Decimal(0))
        if quantity > at_broker:
            outcome.notes.append(
                f"{symbol}: the bot recorded {quantity} but the broker holds {at_broker}; "
                "the bot's record was reduced to match"
            )
            if at_broker > 0:
                own[symbol] = at_broker
            else:
                del own[symbol]

    # A quote for everything the bot holds or wants, and for everything
    # else on the broker: the Risk Engine values the whole account.
    wanted = sorted(set(weights) | set(own) | {s for s, q in paper.positions.items() if q != 0})
    snaps: dict[str, MarketSnapshot] = {}
    for symbol in wanted:
        try:
            snaps[symbol] = await quotes.get_quote(symbol)
        except (DataUnavailableError, VendorError, ValueError) as exc:
            outcome.notes.append(f"{symbol}: no quote ({exc})")
    unpriced = sorted(s for s, q in paper.positions.items() if q != 0 and s not in snaps)
    if unpriced:
        return _finish(
            run,
            outcome,
            LibraryBotRunStatus.FAILED,
            f"cannot value the account: no quote for held {', '.join(unpriced)}; "
            "no price was fabricated",
            clock,
        )
    marks = {s: snaps[s].price for s, q in paper.positions.items() if q != 0}
    prices = {s: snap.price for s, snap in snaps.items()}

    cash = Decimal(bot.cash)
    equity = bot_equity(cash, own, prices)
    peak = max(
        Decimal(bot.peak_equity) if bot.peak_equity is not None else Decimal(bot.capital), equity
    )

    killed = False
    if bot.max_drawdown_pct is not None and peak > 0:
        floor = peak * (Decimal(1) - Decimal(bot.max_drawdown_pct) / Decimal(100))
        if equity <= floor:
            killed = True
            weights = {}
            outcome.notes.append(
                f"kill switch: equity {equity:.2f} is {((equity / peak) - 1) * 100:.1f}% from "
                f"its peak {peak:.2f}, past the {bot.max_drawdown_pct}% limit - selling and pausing"
            )

    stored = bot.target or {}
    refused: dict[str, str] = dict(stored.get("refused", {}))
    new_decision = bot.target is None or decision.decided_on != bot.last_decision_date
    if new_decision or killed:
        refused = {}
    reachable = {s for s in weights if s not in refused}
    mismatch = reachable != set(own)
    lines: list[OrderLine] = []
    pending = False

    if new_decision or mismatch or killed:
        max_age = settings.library_bot_max_quote_age_seconds
        position_cap = settings.risk_max_position_pct_of_equity
        risk_limits = _risk_limits(settings)
        portfolio_limits = _portfolio_limits(settings)
        threshold = max(MIN_TRADE_NOTIONAL, equity * MIN_TRADE_FRACTION)

        plan: list[tuple[str, Decimal]] = []
        for symbol in sorted(set(weights) | set(own)):
            if symbol not in prices:
                pending = pending or symbol in weights
                continue
            have = own.get(symbol, Decimal(0))
            want = quantize(symbol, equity * weights.get(symbol, Decimal(0)) / prices[symbol])
            if symbol in refused and want > have:
                continue
            delta = want - have
            entering_or_leaving = (have == 0) != (want == 0)
            if delta == 0 or (abs(delta) * prices[symbol] < threshold and not entering_or_leaving):
                continue
            plan.append((symbol, delta))
        plan.sort(key=lambda item: item[1])  # sells (negative) before buys

        for symbol, delta in plan:
            snap = snaps[symbol]
            age = (now - snap.as_of).total_seconds()
            side = Side.BUY if delta > 0 else Side.SELL
            quantity = abs(delta)
            line = OrderLine(
                symbol=symbol,
                side=side.value,
                quantity=str(quantity),
                price=str(snap.price),
                quote_as_of=snap.as_of.isoformat(),
                quote_source=snap.source,
                outcome="skipped",
            )
            lines.append(line)
            if age > max_age or age < 0:
                line.detail = (
                    f"quote is {age / 60:.0f} minutes old (limit {max_age / 60:.0f}); the "
                    "market is probably closed - will retry next cycle"
                )
                pending = True
                continue
            if side is Side.BUY:
                account = paper.get_account_state(marks={**marks, symbol: snap.price})
                cap = account.equity * position_cap * CAP_HEADROOM / snap.price
                affordable = cash * CAP_HEADROOM / snap.price
                sized = quantize(symbol, min(quantity, cap, affordable))
                if sized < quantity:
                    line.detail = f"resized from {quantity} to {sized}: limited by " + (
                        "the bot's cash"
                        if affordable <= cap
                        else f"the {position_cap:.0%}-of-account position cap"
                    )
                    quantity = sized
                    line.quantity = str(quantity)
                if quantity <= 0:
                    line.outcome = "skipped"
                    line.detail = (line.detail or "") + "; nothing left to buy"
                    refused[symbol] = "no room under the position cap or the bot's cash"
                    continue
            marks[symbol] = snap.price
            proposal = TradeProposal(
                symbol=symbol,
                side=side,
                quantity=quantity,
                estimated_price=snap.price,
                stop_price=None,
                market_data_as_of=snap.as_of,
            )
            result = await submit_through_risk(
                session,
                bot,  # type: ignore[arg-type]  # only `.broker_id` is read
                paper,
                proposal,
                marks=marks,
                settings=settings,
                risk_limits=risk_limits,
                portfolio_limits=portfolio_limits,
                # The quote's own time, as the intraday bot does with its
                # bar: the bot's (stated, looser) age limit was applied
                # above; the Risk Engine then judges the proposal as of
                # the price it is actually built on.
                now=snap.as_of,
            )
            line.order_id = str(result.order_id) if result.order_id else None
            if result.status is OMSStatus.FILLED and result.fill is not None:
                fill = result.fill
                value = fill.quantity * fill.fill_price
                if side is Side.BUY:
                    own[symbol] = own.get(symbol, Decimal(0)) + fill.quantity
                    cash -= value
                else:
                    remaining = own.get(symbol, Decimal(0)) - fill.quantity
                    if remaining > 0:
                        own[symbol] = remaining
                    else:
                        own.pop(symbol, None)
                    cash += value
                line.outcome = "filled"
                line.quantity = str(fill.quantity)
                line.price = str(fill.fill_price)
                outcome.orders_filled += 1
            else:
                reason = result.risk_decision.detail or (
                    result.portfolio_decision.detail if result.portfolio_decision else None
                )
                line.outcome = "rejected"
                line.detail = "; ".join(p for p in (line.detail, reason) if p) or "rejected"
                outcome.orders_rejected += 1
                if side is Side.BUY:
                    refused[symbol] = reason or "rejected by the Risk Engine"
            if paper.positions.get(symbol, Decimal(0)) == 0:
                marks.pop(symbol, None)

        await save_paper_broker(session, bot.broker_id, paper)

    equity_after = bot_equity(cash, own, prices)
    bot.cash = cash
    bot.positions = {s: str(q) for s, q in own.items()}
    bot.target = {
        "weights": {s: float(w) for s, w in weights.items()},
        "explain": decision.explain,
        "refused": refused,
        "as_of": decision.as_of.isoformat(),
    }
    if not pending:
        bot.last_decision_date = decision.decided_on
    bot.last_equity = equity_after
    bot.peak_equity = max(peak, equity_after)
    bot.updated_at = datetime.now(UTC)
    outcome.equity = equity_after
    run.target = bot.target["weights"]
    run.orders = [line.as_dict() for line in lines]

    if killed:
        bot.status = LibraryBotStatus.PAUSED
        bot.paused_reason = (
            f"Kill switch: drawdown limit of {bot.max_drawdown_pct}% reached on "
            f"{now.date().isoformat()}."
        )

    if not lines:
        headline = (
            "holding the target already"
            if not (new_decision or mismatch or killed)
            else "nothing to trade"
        )
    else:
        headline = (
            f"{outcome.orders_filled} filled, {outcome.orders_rejected} rejected, "
            f"{sum(1 for line in lines if line.outcome == 'skipped')} skipped"
        )
    decided = decision.decided_on.isoformat() if decision.decided_on else "no decision yet"
    return _finish(
        run,
        outcome,
        LibraryBotRunStatus.SUCCEEDED,
        f"{headline} (strategy decision {decided}, data to {decision.as_of.isoformat()})",
        clock,
    )
