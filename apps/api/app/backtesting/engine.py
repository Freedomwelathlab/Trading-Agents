"""Orchestrates one backtest run: real historical closes (HistoryProvider,
D021) -> the hard-coded SMA(20)-crossover strategy (strategy.py) -> the real
deterministic Risk Engine (risk.engine.evaluate_trade, D004) -> the real
trade-path Portfolio Manager (portfolio_manager.manager.decide, D029) ->
the real PaperBrokerAdapter fill math (execution.paper_broker, D014) -> a
BacktestResult. See docs/DECISIONS.md D025 and D035.

That RISK -> PORTFOLIO -> BROKER sequence is replicated here inline rather
than by calling oms.service.submit_trade(): the OMS path is coupled to
database persistence and audit rows, and a backtest deliberately writes
nothing (D025). The sequence itself - including the property that a
Portfolio-Manager MODIFY is re-evaluated by the Risk Engine before any fill
- mirrors submit_trade() exactly; tests/backtesting/test_engine_portfolio_manager.py
pins that, mirroring tests/oms/test_service_portfolio_manager.py.

A fresh PaperBrokerAdapter is constructed here, in memory, for every call -
this module never imports execution.persistence's load_paper_broker /
save_paper_broker, so it is structurally impossible for a backtest run to
read or write a real broker's persisted cash/position rows. The
PortfolioState the Portfolio Manager sees is built from that same
disposable broker at each simulated decision point, never from a real
broker's rows.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from apps.api.app.backtesting.errors import InsufficientHistoryError, UnsupportedDateRangeError
from apps.api.app.backtesting.metrics import (
    RoundTrip,
    compute_max_drawdown_pct,
    compute_total_return_pct,
    compute_win_rate_pct,
)
from apps.api.app.backtesting.models import BacktestRequest, BacktestResult, EquityPoint
from apps.api.app.backtesting.strategy import SMA_PERIOD, Signal, generate_signals
from apps.api.app.execution.broker import OrderRequest
from apps.api.app.execution.paper_broker import (
    InsufficientFundsError,
    InsufficientPositionError,
    PaperBrokerAdapter,
)
from apps.api.app.marketdata.history_provider import HistoryProvider
from apps.api.app.portfolio_manager.manager import decide as portfolio_decide
from apps.api.app.portfolio_manager.manager import portfolio_state_from_positions
from apps.api.app.portfolio_manager.models import PortfolioAction, PortfolioLimits
from apps.api.app.risk.engine import evaluate_trade
from apps.api.app.risk.models import BlockReason, RiskLimits, Side, TradeProposal

_CLOSE_TIME = time(21, 0, tzinfo=UTC)
"""Assumed close timestamp attached to each daily close for the Risk
Engine's market-data-freshness check. HistoryProvider (D021) gives closes,
not per-close timestamps, so this is a fixed, documented approximation -
never a fabricated *price*, only a fixed clock time paired with a real
close - and `now` is passed as the same timestamp on every evaluate_trade
call below, so the staleness check always sees age=0 rather than
spuriously rejecting historical data as stale."""


def _trading_days_between(start_date: date, end_date: date) -> int:
    """Count of Mon-Fri calendar days in [start_date, end_date] inclusive.
    An approximation - it does not know about market holidays - used only
    to size the HistoryProvider request; see docs/DECISIONS.md D025."""
    count = 0
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def _label_trading_days(*, end_date: date, count: int) -> list[date]:
    """Returns `count` dates, oldest-first, ending at end_date, skipping
    Saturdays/Sundays. Does not know about market holidays - see
    docs/DECISIONS.md D025's HistoryProvider limitation note. This labels
    real closes with a best-effort trading-day calendar; it never invents
    a price."""
    dates: list[date] = []
    current = end_date
    while len(dates) < count:
        if current.weekday() < 5:
            dates.append(current)
        current -= timedelta(days=1)
    dates.reverse()
    return dates


def _most_recent_trading_day(day: date) -> date:
    """Rolls a calendar date back to the most recent Mon-Fri date (itself,
    if it's already a weekday). Used only to derive the *default* "today"
    from the real wall clock - a caller-supplied `today` (tests pinning a
    specific date) is used exactly as given, never adjusted, since those
    callers already choose a weekday deliberately."""
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


@dataclass(frozen=True)
class _TradeAttempt:
    """What one simulated trade decision did. `filled_quantity` is
    Decimal(0) whenever nothing reached the broker, whatever stopped it."""

    filled_quantity: Decimal
    portfolio_modified: bool = False
    portfolio_modify_risk_blocked: bool = False
    portfolio_rejected: bool = False


async def run_backtest(
    request: BacktestRequest,
    *,
    history_provider: HistoryProvider,
    risk_limits: RiskLimits,
    portfolio_limits: PortfolioLimits | None = None,
    today: date | None = None,
) -> BacktestResult:
    """`portfolio_limits` is optional for exactly the reason
    oms.service.submit_trade()'s own `portfolio_limits` is (D029): omitting
    it skips the Portfolio Manager and restores pre-D035 behaviour, which
    cannot make a simulated trade less safe because the Risk Engine still
    gated it and the Portfolio Manager can only ever shrink or stop a
    trade. apps/api/app/api/routes/backtests.py always supplies it, built
    from the same Settings.portfolio_* values the live trade path uses."""
    today = today or _most_recent_trading_day(datetime.now(UTC).date())
    if request.end_date != today:
        raise UnsupportedDateRangeError(
            f"end_date must be {today.isoformat()} (the most recent available trading "
            f"day) - HistoryProvider only exposes the most recent N daily closes as of "
            f"now, see docs/DECISIONS.md D025."
        )

    requested_days = _trading_days_between(request.start_date, request.end_date)
    total_count = requested_days + SMA_PERIOD

    closes = await history_provider.get_daily_closes(request.symbol, count=total_count)
    if len(closes) < total_count:
        raise InsufficientHistoryError(
            f"Requested a {requested_days}-trading-day backtest window plus a "
            f"{SMA_PERIOD}-day SMA warmup ({total_count} closes total) for "
            f"{request.symbol!r}, but only {len(closes)} were available."
        )
    # HistoryProvider may return more than requested - keep exactly the
    # most recent total_count so the window is deterministic regardless of
    # how much the vendor happened to return.
    closes = closes[-total_count:]
    dates = _label_trading_days(end_date=request.end_date, count=total_count)

    signals = generate_signals(closes, period=SMA_PERIOD)

    broker = PaperBrokerAdapter(starting_cash=request.starting_cash)
    equity_curve: list[EquityPoint] = []
    round_trips: list[RoundTrip] = []

    # Entry price of the currently-open position, if any. The v1 strategy
    # only ever opens a position from flat with a single buy attempt (see
    # the BUY branch's `held == 0` guard below), so this is always exactly
    # that one fill's price - never a multi-fill weighted average.
    open_entry_price: Decimal | None = None

    portfolio_modified = 0
    portfolio_modify_risk_blocked = 0
    portfolio_rejected = 0

    # Only the requested window (post-warmup) appears in the output.
    for i in range(SMA_PERIOD, total_count):
        current_date = dates[i]
        price = closes[i]
        as_of = datetime.combine(current_date, _CLOSE_TIME)
        signal = signals[i]
        held = broker.positions.get(request.symbol, Decimal(0))

        if signal is Signal.BUY and held == 0:
            account = broker.get_account_state(marks={request.symbol: price})
            quantity = (account.cash / price).to_integral_value(rounding="ROUND_FLOOR")
            if quantity > 0:
                attempt = _attempt_trade(
                    broker=broker,
                    symbol=request.symbol,
                    side=Side.BUY,
                    quantity=quantity,
                    price=price,
                    as_of=as_of,
                    risk_limits=risk_limits,
                    portfolio_limits=portfolio_limits,
                )
                portfolio_modified += int(attempt.portfolio_modified)
                portfolio_modify_risk_blocked += int(attempt.portfolio_modify_risk_blocked)
                portfolio_rejected += int(attempt.portfolio_rejected)
                if attempt.filled_quantity > 0:
                    open_entry_price = price

        elif signal is Signal.SELL and held > 0:
            attempt = _attempt_trade(
                broker=broker,
                symbol=request.symbol,
                side=Side.SELL,
                quantity=held,
                price=price,
                as_of=as_of,
                risk_limits=risk_limits,
                portfolio_limits=portfolio_limits,
            )
            portfolio_modified += int(attempt.portfolio_modified)
            portfolio_modify_risk_blocked += int(attempt.portfolio_modify_risk_blocked)
            portfolio_rejected += int(attempt.portfolio_rejected)
            if attempt.filled_quantity > 0 and open_entry_price is not None:
                remaining = broker.positions.get(request.symbol, Decimal(0))
                if remaining == 0:
                    round_trips.append(RoundTrip(entry_price=open_entry_price, exit_price=price))
                    open_entry_price = None

        equity = broker.get_account_state(marks={request.symbol: price}).equity
        equity_curve.append(EquityPoint(date=current_date, equity=equity))

    final_equity = equity_curve[-1].equity if equity_curve else request.starting_cash

    return BacktestResult(
        symbol=request.symbol,
        start_date=request.start_date,
        end_date=request.end_date,
        starting_cash=request.starting_cash,
        final_equity=final_equity,
        total_return_pct=compute_total_return_pct(
            starting_cash=request.starting_cash, final_equity=final_equity
        ),
        num_trades=len(round_trips),
        win_rate_pct=compute_win_rate_pct(round_trips),
        max_drawdown_pct=compute_max_drawdown_pct([p.equity for p in equity_curve]),
        equity_curve=equity_curve,
        portfolio_modified_trades=portfolio_modified,
        portfolio_modify_risk_blocked_trades=portfolio_modify_risk_blocked,
        portfolio_rejected_trades=portfolio_rejected,
    )


def _attempt_trade(
    *,
    broker: PaperBrokerAdapter,
    symbol: str,
    side: Side,
    quantity: Decimal,
    price: Decimal,
    as_of: datetime,
    risk_limits: RiskLimits,
    portfolio_limits: PortfolioLimits | None = None,
) -> _TradeAttempt:
    """One simulated trade decision, run through the same gate sequence a
    real trade goes through in oms.service.submit_trade(): the real Risk
    Engine, then - only on a risk-approved proposal - the real trade-path
    Portfolio Manager, then the real PaperBrokerAdapter fill math.

    Risk-engine retry: if the engine blocks on EXCEEDS_MAX_POSITION_SIZE
    and supplies max_quantity_allowed, this retries exactly once at that
    quantity (a deterministic policy choice made here, in the caller - the
    engine itself never resizes an order, per its own docstring).

    Portfolio Manager: a REJECT ends the attempt with no fill, and is NOT
    retried at a smaller size - the retry above exists solely for the risk
    engine's own reported cap, and submit_trade() likewise returns on a
    portfolio rejection rather than renegotiating. A MODIFY's resized
    quantity is passed back through evaluate_trade() before the fill, so no
    quantity this system produced itself reaches the (simulated) broker
    ungated - the same load-bearing invariant D029 pinned for the live
    path.
    """
    now = as_of.replace(tzinfo=UTC) if as_of.tzinfo is None else as_of
    for attempt_quantity in _candidate_quantities(quantity, risk_limits, price, side, broker):
        if attempt_quantity <= 0:
            continue
        account = broker.get_account_state(marks={symbol: price})
        proposal = TradeProposal(
            symbol=symbol,
            side=side,
            quantity=attempt_quantity,
            estimated_price=price,
            stop_price=None,
            market_data_as_of=now,
        )
        decision = evaluate_trade(proposal, account, risk_limits, now=now)
        if decision.approved:
            return _apply_portfolio_gate_and_fill(
                broker=broker,
                proposal=proposal,
                account_marks={symbol: price},
                risk_limits=risk_limits,
                portfolio_limits=portfolio_limits,
                now=now,
            )
        blocked_on_size = decision.reason is BlockReason.EXCEEDS_MAX_POSITION_SIZE
        if blocked_on_size and decision.max_quantity_allowed:
            # single retry at the engine-reported max quantity, handled by
            # the loop's next candidate (see _candidate_quantities).
            continue
        return _TradeAttempt(filled_quantity=Decimal(0))
    return _TradeAttempt(filled_quantity=Decimal(0))


def _apply_portfolio_gate_and_fill(
    *,
    broker: PaperBrokerAdapter,
    proposal: TradeProposal,
    account_marks: dict[str, Decimal],
    risk_limits: RiskLimits,
    portfolio_limits: PortfolioLimits | None,
    now: datetime,
) -> _TradeAttempt:
    """The PORTFOLIO MANAGER -> BROKER half of the sequence, on a proposal
    the Risk Engine has already approved. The PortfolioState is built from
    the run's own disposable in-memory broker at this point in the
    simulated timeline (D025) - never from a real broker's persisted rows,
    and never fetched over a network, keeping the replay loop pure."""
    effective = proposal
    modified = False

    if portfolio_limits is not None:
        portfolio = portfolio_state_from_positions(
            positions=broker.positions, marks=account_marks, cash=broker.cash
        )
        portfolio_decision = portfolio_decide(proposal, portfolio, portfolio_limits)

        if portfolio_decision.action is PortfolioAction.REJECT:
            return _TradeAttempt(filled_quantity=Decimal(0), portfolio_rejected=True)

        if portfolio_decision.action is PortfolioAction.MODIFY:
            modified = True
            effective = proposal.model_copy(
                update={"quantity": portfolio_decision.approved_quantity}
            )
            # The resized proposal is a new proposal, and every proposal
            # goes through the Risk Engine - including one this system
            # produced itself. Mirrors oms/service.py's re-gate (D029).
            account = broker.get_account_state(marks=account_marks)
            regate = evaluate_trade(effective, account, risk_limits, now=now)
            if not regate.approved:
                return _TradeAttempt(
                    filled_quantity=Decimal(0),
                    portfolio_modified=True,
                    portfolio_modify_risk_blocked=True,
                )

    try:
        broker.submit_order(
            OrderRequest(
                symbol=effective.symbol, side=effective.side, quantity=effective.quantity
            ),
            market_price=effective.estimated_price,
        )
    except (InsufficientFundsError, InsufficientPositionError):
        return _TradeAttempt(filled_quantity=Decimal(0), portfolio_modified=modified)
    return _TradeAttempt(filled_quantity=effective.quantity, portfolio_modified=modified)


def _candidate_quantities(
    quantity: Decimal,
    risk_limits: RiskLimits,
    price: Decimal,
    side: Side,
    broker: PaperBrokerAdapter,
) -> list[Decimal]:
    """The initial proposed quantity, followed by the engine-computed max
    allowed under max_position_pct_of_equity - computed here rather than
    re-deriving it from a rejection, so exactly two attempts are made
    regardless of the rejection reason, no unbounded retry loop."""
    if side is Side.SELL:
        # A sell's quantity is capped by what's actually held, not by
        # max_position_pct_of_equity (that limit governs how large a new
        # position may be, not how much of an existing one may be closed).
        # No account lookup needed here, which also avoids requiring a mark
        # for a symbol this function doesn't otherwise know.
        return [quantity]

    account = broker.get_account_state(marks={})
    # Safe with an empty marks dict: this branch only ever runs when the
    # caller is flat in `symbol` (engine.py only calls _attempt_trade with
    # side=BUY when held == 0), so there is no open position requiring a
    # mark to value.
    max_notional = account.equity * risk_limits.max_position_pct_of_equity
    max_qty = (max_notional / price).to_integral_value(rounding="ROUND_FLOOR")
    return [quantity, max_qty] if max_qty != quantity else [quantity]
