"""Orchestrates one backtest run: real historical closes (HistoryProvider,
D021) -> the hard-coded SMA(20)-crossover strategy (strategy.py) -> the real
deterministic Risk Engine (risk.engine.evaluate_trade, D004) -> the real
PaperBrokerAdapter fill math (execution.paper_broker, D014) -> a
BacktestResult. See docs/DECISIONS.md D025.

A fresh PaperBrokerAdapter is constructed here, in memory, for every call -
this module never imports execution.persistence's load_paper_broker /
save_paper_broker, so it is structurally impossible for a backtest run to
read or write a real broker's persisted cash/position rows.
"""

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


async def run_backtest(
    request: BacktestRequest,
    *,
    history_provider: HistoryProvider,
    risk_limits: RiskLimits,
    today: date | None = None,
) -> BacktestResult:
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
                filled_qty = _attempt_trade(
                    broker=broker,
                    symbol=request.symbol,
                    side=Side.BUY,
                    quantity=quantity,
                    price=price,
                    as_of=as_of,
                    risk_limits=risk_limits,
                )
                if filled_qty > 0:
                    open_entry_price = price

        elif signal is Signal.SELL and held > 0:
            filled_qty = _attempt_trade(
                broker=broker,
                symbol=request.symbol,
                side=Side.SELL,
                quantity=held,
                price=price,
                as_of=as_of,
                risk_limits=risk_limits,
            )
            if filled_qty > 0 and open_entry_price is not None:
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
) -> Decimal:
    """Runs one proposal through the real Risk Engine and, if approved,
    the real PaperBrokerAdapter fill math. If the engine blocks on
    EXCEEDS_MAX_POSITION_SIZE and supplies max_quantity_allowed, retries
    exactly once at that quantity (a deterministic policy choice made
    here, in the caller - the engine itself never resizes an order, per
    its own docstring). Returns the quantity actually filled, or
    Decimal(0) if no trade was made."""
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
            market_data_as_of=as_of.replace(tzinfo=UTC) if as_of.tzinfo is None else as_of,
        )
        decision = evaluate_trade(
            proposal,
            account,
            risk_limits,
            now=as_of.replace(tzinfo=UTC) if as_of.tzinfo is None else as_of,
        )
        if decision.approved:
            try:
                broker.submit_order(
                    OrderRequest(symbol=symbol, side=side, quantity=attempt_quantity),
                    market_price=price,
                )
            except (InsufficientFundsError, InsufficientPositionError):
                return Decimal(0)
            return attempt_quantity
        blocked_on_size = decision.reason is BlockReason.EXCEEDS_MAX_POSITION_SIZE
        if blocked_on_size and decision.max_quantity_allowed:
            # single retry at the engine-reported max quantity, handled by
            # the loop's next candidate (see _candidate_quantities).
            continue
        return Decimal(0)
    return Decimal(0)


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
