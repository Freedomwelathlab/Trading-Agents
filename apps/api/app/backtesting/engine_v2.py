"""Orchestrates one PERSISTED backtest of a user-authored StrategyVersion
(Phase 55): real historical OHLCV bars (HistoricalBarProvider, D070) ->
the definition-driven signal evaluator (backtesting/executor.py) -> the
real deterministic Risk Engine (risk.engine.evaluate_trade, D004) -> the
real trade-path Portfolio Manager (portfolio_manager.manager.decide, D029)
-> the real PaperBrokerAdapter fill math (execution.paper_broker, D014) ->
a `BacktestRun` row plus its equity curve and trades.

**A SIBLING of apps/api/app/backtesting/engine.py, never a replacement.**
That module is D025's v1 engine and stays exactly as it is, forever: it is
what `POST /backtests` runs and what this module's numbers are measured
against. Nothing here edits it. What this module generalizes is the three
things v1 hard-codes - the SMA(20) strategy (now any validated definition),
"all available cash" sizing (now `position_sizing`), and "the window must
end today" (now an arbitrary historical window over the persisted bar
store) - and the one thing v1 deliberately does not do at all: persist.

**The RISK -> PORTFOLIO -> BROKER sequence is REUSED, not re-derived.**
`_attempt_trade` is imported from engine.py below rather than copied. The
leading underscore is a module-internal convention, not an access control,
and reusing the exact, already-tested sequence is explicitly preferred here
over duplicating it: that function is what guarantees a Portfolio-Manager
MODIFY is re-evaluated by the Risk Engine before any fill, and a second
copy of it could drift out of agreement with the first while both kept
passing their own tests.

A fresh PaperBrokerAdapter is constructed per call, in memory, exactly as
in v1 - this module never imports execution.persistence, so it remains
structurally impossible for a backtest to read or write a real broker's
persisted cash/position rows. The only rows this module writes are its own
`backtest_runs` / `backtest_equity_points` / `backtest_trades`.

**Real dates, real gaps, never a fabricated bar.** Bars carry their own
vendor timestamps here, so unlike v1 - which had to pair
HistoryProvider's untimestamped closes with an assumed close time and a
best-effort weekday calendar - nothing about a day is inferred. A weekend
or a market holiday simply has no bar and therefore no equity point; it is
never filled in.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

# `_attempt_trade` is imported ACROSS MODULES despite its leading
# underscore, deliberately: it is the exact RISK -> PORTFOLIO -> BROKER
# replay sequence D025/D035 already pinned with tests, and reusing it is
# preferred over duplicating it (a second copy could drift). It reaches
# engine.py's `_apply_portfolio_gate_and_fill` internally, which is why
# that half of the sequence is not imported separately here.
from apps.api.app.backtesting.engine import _attempt_trade
from apps.api.app.backtesting.errors import InsufficientHistoryError
from apps.api.app.backtesting.executor import generate_signals
from apps.api.app.backtesting.metrics import (
    RoundTrip,
    compute_max_drawdown_pct,
    compute_total_return_pct,
    compute_win_rate_pct,
)
from apps.api.app.backtesting.strategy import Signal
from apps.api.app.core.logging import get_logger
from apps.api.app.db.models import (
    BacktestEquityPoint,
    BacktestRun,
    BacktestRunStatus,
    BacktestTrade,
    StrategyVersion,
)
from apps.api.app.execution.paper_broker import PaperBrokerAdapter
from apps.api.app.marketdata.bar_provider import Bar, HistoricalBarProvider
from apps.api.app.portfolio_manager.models import PortfolioLimits
from apps.api.app.risk.models import RiskLimits, Side
from apps.api.app.strategies.models import PositionSizingType

logger = get_logger(__name__)

_ERROR_DETAIL_MAX_LENGTH = 500
"""`backtest_runs.error_detail` is String(500). A failure message is
truncated to fit rather than being allowed to fail the very write that
records the failure."""


def _bar_date(bar: Bar) -> date:
    """A bar's calendar day in UTC. Normalized through `astimezone(UTC)`
    rather than read off `ts` as-is, matching how
    marketdata/ingestion/backfill.py derives a bar's date and how
    MarketDataStore's own range filter is built - so a vendor timestamp in
    a non-UTC zone lands on the same day here as it does there."""
    return bar.ts.astimezone(UTC).date()


def _warmup_bar_count(definition: dict) -> int:
    """How many bars must precede `start_date` for every declared indicator
    to be defined on the window's very first bar.

    `max(period) + 1`. The `+1` covers RSI needing `period + 1` bars for its
    first value without this function having to branch on indicator type -
    one bar of slack costs nothing for SMA, and getting a type-specific rule
    subtly wrong here would silently shorten a warmup rather than fail. It
    also covers the crossing operators, which compare a bar against the one
    before it.

    A definition with no indicators at all still warms up 1 bar, for that
    same crossing-operator reason.
    """
    periods = [
        indicator["period"]
        for indicator in definition.get("indicators", [])
        if isinstance(indicator, dict) and isinstance(indicator.get("period"), int)
    ]
    return max(periods, default=0) + 1


@dataclass(frozen=True)
class _OpenPosition:
    """The still-open leg of a round trip. Only ever set from a real fill,
    so `quantity` is what the broker actually filled - which is not
    necessarily what the strategy proposed, since the Risk Engine and the
    Portfolio Manager may both have shrunk it on the way through."""

    entry_date: date
    entry_price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class _CompletedTrade:
    """One closed round trip, in the shape `backtest_trades` stores."""

    entry_date: date
    entry_price: Decimal
    exit_date: date
    exit_price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class _Replay:
    """Everything one completed replay produced, before any of it is
    written. Kept as a value so the persistence step below has a single
    object to write rather than five parallel locals."""

    equity_curve: list[tuple[date, Decimal]]
    round_trips: list[RoundTrip]
    trades: list[_CompletedTrade]
    final_equity: Decimal


def _desired_quantity(
    *, sizing: dict, cash: Decimal, equity: Decimal, price: Decimal
) -> Decimal:
    """Whole shares to propose for an entry, per the definition's
    `position_sizing` block (Phase 54's closed vocabulary).

    - `all_in`: `cash / price` - identical to v1's hard-coded behaviour, so
      an `all_in` definition reproduces D025's engine exactly.
    - `fixed_fraction`: `equity * fraction / price`. Deliberately against
      EQUITY, not cash: "risk 10% of the portfolio" is a statement about
      portfolio size, and sizing off cash would silently shrink every entry
      as positions accumulate.
    - `fixed_notional`: `min(cash, amount) / price`. Capped by cash so an
      amount larger than the account proposes what can actually be afforded
      rather than a quantity certain to be refused.

    Floored to a whole share (`ROUND_FLOOR`), exactly as v1 floors its own
    all-cash quantity - this system does not model fractional shares
    anywhere, and rounding UP would propose a trade the account cannot pay
    for. A sizing that floors to 0 simply makes no trade; that is a real
    answer, not an error.

    `fraction` / `amount` arrive as JSON numbers (possibly floats), so they
    are converted through `Decimal(str(...))` rather than `Decimal(float)` -
    the latter would carry a binary-float artifact into a money calculation,
    which this codebase does not permit anywhere.
    """
    sizing_type = PositionSizingType(sizing["type"])
    if sizing_type is PositionSizingType.ALL_IN:
        raw = cash / price
    elif sizing_type is PositionSizingType.FIXED_FRACTION:
        raw = equity * Decimal(str(sizing["fraction"])) / price
    else:
        raw = min(cash, Decimal(str(sizing["amount"]))) / price
    return raw.to_integral_value(rounding="ROUND_FLOOR")


async def _load_warmup_and_window(
    *,
    bar_provider: HistoricalBarProvider,
    symbol: str,
    bar_interval: str,
    start_date: date,
    end_date: date,
    warmup_bars: int,
) -> tuple[list[Bar], list[Bar]]:
    """ONE call to the bar provider covering warmup and window together,
    split into `(warmup, window)` on the real dates the bars carry.

    The fetch reaches back `warmup_bars * 2 + 10` CALENDAR days before
    `start_date` to find `warmup_bars` TRADING bars. That buffer is the same
    posture engine.py's `_trading_days_between` / `_label_trading_days`
    already document for v1 - it does not know about market holidays, and is
    deliberately generous enough to cover weekends rather than pretending to
    a calendar it does not have. The difference is that this function then
    counts what actually came back instead of assuming; a buffer that turns
    out to be too small produces an `InsufficientHistoryError` naming the
    real numbers, never a silently shortened warmup.

    Raises `InsufficientHistoryError` - reusing backtesting/errors.py's,
    never a second error type meaning the same thing - when fewer than
    `warmup_bars` bars precede `start_date`, or when the requested window
    contains no bars at all. The window one matters as much as the warmup:
    an empty window would otherwise produce a SUCCEEDED run reporting a 0%
    return, which reads as "the strategy did nothing" when the fact is "no
    data exists here" (docs/TRADING_SAFETY.md's no-fabrication rule).

    A window SHORTER than the calendar range is not an error and never
    triggers either check - weekends and market holidays are real gaps, and
    a missing day's bar is never invented to fill one.
    """
    fetch_start = start_date - timedelta(days=warmup_bars * 2 + 10)
    bars = await bar_provider.get_bars(
        symbol, bar_interval=bar_interval, start_date=fetch_start, end_date=end_date
    )

    prefix = [bar for bar in bars if _bar_date(bar) < start_date]
    window = [bar for bar in bars if _bar_date(bar) >= start_date]

    if len(prefix) < warmup_bars:
        raise InsufficientHistoryError(
            f"{symbol!r} needs {warmup_bars} {bar_interval} bar(s) of indicator warmup before "
            f"{start_date.isoformat()}, but only {len(prefix)} were found in the persisted bar "
            f"store searching back to {fetch_start.isoformat()}. Backfill more history "
            "(POST /admin/market-data/backfill) or start the window later."
        )
    if not window:
        raise InsufficientHistoryError(
            f"No {bar_interval} bars are persisted for {symbol!r} between "
            f"{start_date.isoformat()} and {end_date.isoformat()}, so there is no window to "
            "replay. Backfill this range (POST /admin/market-data/backfill) first."
        )

    return prefix[-warmup_bars:], window


def _replay_window(
    *,
    symbol: str,
    definition: dict,
    warmup: list[Bar],
    window: list[Bar],
    starting_cash: Decimal,
    risk_limits: RiskLimits,
    portfolio_limits: PortfolioLimits | None,
) -> _Replay:
    """The simulation itself. Pure in the sense that matters: no session, no
    provider, no I/O - it takes bars and limits and returns numbers.

    Signals are generated over warmup + window TOGETHER (the evaluator needs
    the warmup to have an indicator value at all on the window's first bar),
    but only the window portion is ever replayed or reported - the same
    "only the requested window appears in the output" rule engine.py states,
    here against real calendar bars instead of an offset-counted list.
    """
    all_bars = warmup + window
    signals = generate_signals(all_bars, definition)
    sizing = definition["position_sizing"]

    broker = PaperBrokerAdapter(starting_cash=starting_cash)
    equity_curve: list[tuple[date, Decimal]] = []
    round_trips: list[RoundTrip] = []
    trades: list[_CompletedTrade] = []
    open_position: _OpenPosition | None = None

    for index in range(len(warmup), len(all_bars)):
        bar = all_bars[index]
        current_date = _bar_date(bar)
        price = bar.close
        # The bar's OWN vendor timestamp, not an assumed close time. v1 had
        # to invent one because HistoryProvider gives untimestamped closes
        # (see engine.py's _CLOSE_TIME); a persisted Bar carries the real
        # thing. It is passed as both the proposal's market_data_as_of and
        # the evaluation `now`, so the Risk Engine's freshness check sees
        # age=0 rather than rejecting historical data as stale.
        as_of = bar.ts
        signal = signals[index]
        held = broker.positions.get(symbol, Decimal(0))

        if signal is Signal.BUY and held == 0:
            account = broker.get_account_state(marks={symbol: price})
            quantity = _desired_quantity(
                sizing=sizing, cash=account.cash, equity=account.equity, price=price
            )
            if quantity > 0:
                attempt = _attempt_trade(
                    broker=broker,
                    symbol=symbol,
                    side=Side.BUY,
                    quantity=quantity,
                    price=price,
                    as_of=as_of,
                    risk_limits=risk_limits,
                    portfolio_limits=portfolio_limits,
                )
                if attempt.filled_quantity > 0:
                    open_position = _OpenPosition(
                        entry_date=current_date,
                        entry_price=price,
                        # What actually FILLED, which the Risk Engine or the
                        # Portfolio Manager may have shrunk below `quantity`.
                        quantity=attempt.filled_quantity,
                    )

        elif signal is Signal.SELL and held > 0:
            # A SELL while flat is a no-op, exactly as in v1 - this branch's
            # `held > 0` guard is the whole mechanism, and nothing reaches
            # _attempt_trade on such a bar.
            attempt = _attempt_trade(
                broker=broker,
                symbol=symbol,
                side=Side.SELL,
                quantity=held,
                price=price,
                as_of=as_of,
                risk_limits=risk_limits,
                portfolio_limits=portfolio_limits,
            )
            if attempt.filled_quantity > 0 and open_position is not None:
                remaining = broker.positions.get(symbol, Decimal(0))
                if remaining == 0:
                    round_trips.append(
                        RoundTrip(entry_price=open_position.entry_price, exit_price=price)
                    )
                    trades.append(
                        _CompletedTrade(
                            entry_date=open_position.entry_date,
                            entry_price=open_position.entry_price,
                            exit_date=current_date,
                            exit_price=price,
                            quantity=open_position.quantity,
                        )
                    )
                    open_position = None

        equity = broker.get_account_state(marks={symbol: price}).equity
        equity_curve.append((current_date, equity))

    final_equity = equity_curve[-1][1] if equity_curve else starting_cash
    return _Replay(
        equity_curve=equity_curve,
        round_trips=round_trips,
        trades=trades,
        final_equity=final_equity,
    )


def _persist_children(session: AsyncSession, run: BacktestRun, replay: _Replay) -> None:
    """One `BacktestEquityPoint` per bar in the output window and one
    `BacktestTrade` per completed round trip, added in bulk.

    Every trade row carries `side="buy"`. That is a KNOWN SCOPE LIMIT of
    this phase, not a bug and not a placeholder: the replay above only ever
    opens a position from flat with a BUY (mirroring v1 exactly), so every
    round trip that exists is a long one, and recording anything else would
    be recording something that did not happen. Short entries are a future
    phase's change to the replay loop; this column already has room for
    them.
    """
    session.add_all(
        [
            BacktestEquityPoint(
                id=uuid.uuid4(), backtest_run_id=run.id, date=day, equity=equity
            )
            for day, equity in replay.equity_curve
        ]
    )
    session.add_all(
        [
            BacktestTrade(
                id=uuid.uuid4(),
                backtest_run_id=run.id,
                side=Side.BUY.value,
                entry_date=trade.entry_date,
                entry_price=trade.entry_price,
                exit_date=trade.exit_date,
                exit_price=trade.exit_price,
                quantity=trade.quantity,
                return_pct=(trade.exit_price - trade.entry_price)
                / trade.entry_price
                * Decimal(100),
            )
            for trade in replay.trades
        ]
    )


async def run_strategy_backtest(
    *,
    session: AsyncSession,
    strategy_version: StrategyVersion,
    symbol: str,
    bar_interval: str,
    start_date: date,
    end_date: date,
    starting_cash: Decimal,
    bar_provider: HistoricalBarProvider,
    risk_limits: RiskLimits,
    portfolio_limits: PortfolioLimits | None,
    requested_by_user_id: uuid.UUID | None,
) -> BacktestRun:
    """Runs `strategy_version` over `[start_date, end_date]` and returns the
    committed `BacktestRun` row, always in a terminal status.

    `strategy_version` MUST already be `VALIDATED`; that is the caller's
    responsibility to check, and
    apps/api/app/api/routes/strategy_backtests.py answers 409 before ever
    calling this. It is what makes `executor.generate_signals`'s
    "`validate_definition(definition) == []`" precondition true, and this
    function does not re-check it for the same reason the evaluator does
    not: one enforcement point, not two that can disagree.

    **This function RETURNS a failed run; it does not raise one.** That is a
    deliberate difference from engine.py's `run_backtest`, which raises
    `InsufficientHistoryError` for the route to translate into a 400. v1
    raises because it has nothing to persist either way - a failed v1
    backtest leaves no trace at all, so an exception loses nothing. v2
    always has a row to finish: it created one before doing any work, and a
    failed attempt is still a real, auditable resource that says what was
    asked for, when, by whom, and exactly why it could not be answered. So
    a failure sets `status=FAILED`, `error_detail`, `completed_at`, commits,
    and hands the row back - `run_backfill_job`'s posture (Phase 53/D070),
    not v1's.

    `portfolio_limits` is optional for exactly the reason
    `run_backtest`'s and `oms.service.submit_trade`'s are (D029): omitting
    it skips the Portfolio Manager and cannot make a simulated trade less
    safe, because the Risk Engine still gated it and the Portfolio Manager
    can only ever shrink or stop a trade. The route always supplies it,
    built from the same `Settings.portfolio_*` values the live trade path
    uses.
    """
    run = BacktestRun(
        id=uuid.uuid4(),
        strategy_version_id=strategy_version.id,
        requested_by_user_id=requested_by_user_id,
        symbol=symbol,
        bar_interval=bar_interval,
        start_date=start_date,
        end_date=end_date,
        starting_cash=starting_cash,
        status=BacktestRunStatus.RUNNING,
    )
    session.add(run)
    # Flushed before any work so the row - and its id - exist for the whole
    # duration of the run, which is what makes a failure below something
    # that can be recorded rather than something that vanishes.
    await session.flush()

    definition = strategy_version.definition
    try:
        warmup, window = await _load_warmup_and_window(
            bar_provider=bar_provider,
            symbol=symbol,
            bar_interval=bar_interval,
            start_date=start_date,
            end_date=end_date,
            warmup_bars=_warmup_bar_count(definition),
        )
        replay = _replay_window(
            symbol=symbol,
            definition=definition,
            warmup=warmup,
            window=window,
            starting_cash=starting_cash,
            risk_limits=risk_limits,
            portfolio_limits=portfolio_limits,
        )
    except Exception as exc:
        # Broad on purpose. Every failure here - a data gap, a vendor error,
        # a definition that somehow reached this function without being
        # valid - is a real outcome of a run this system was asked to
        # perform, and the row already exists to record it. Swallowing it
        # into a FAILED row with the real message is strictly more useful
        # than a 500 that leaves a RUNNING row stranded forever.
        run.status = BacktestRunStatus.FAILED
        run.error_detail = str(exc)[:_ERROR_DETAIL_MAX_LENGTH]
        run.completed_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(run)
        logger.info(
            "backtest_run_failed",
            backtest_run_id=str(run.id),
            strategy_version_id=str(strategy_version.id),
            symbol=symbol,
            error_type=type(exc).__name__,
        )
        return run

    run.final_equity = replay.final_equity
    run.total_return_pct = compute_total_return_pct(
        starting_cash=starting_cash, final_equity=replay.final_equity
    )
    run.max_drawdown_pct = compute_max_drawdown_pct(
        [equity for _day, equity in replay.equity_curve]
    )
    run.win_rate_pct = compute_win_rate_pct(replay.round_trips)
    run.num_trades = len(replay.round_trips)
    run.status = BacktestRunStatus.SUCCEEDED
    run.completed_at = datetime.now(UTC)
    _persist_children(session, run, replay)
    await session.commit()
    # Re-read so the returned row carries what the database actually stores
    # - the metric columns are NUMERIC(10,4)/(20,6), so a Decimal with more
    # places than that is rounded on write, and a caller must see the stored
    # value rather than the pre-write one.
    await session.refresh(run)

    logger.info(
        "backtest_run_succeeded",
        backtest_run_id=str(run.id),
        strategy_version_id=str(strategy_version.id),
        symbol=symbol,
        bar_interval=bar_interval,
        equity_points=len(replay.equity_curve),
        num_trades=len(replay.round_trips),
    )
    return run
