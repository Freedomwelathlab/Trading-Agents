"""Intraday reversal backtest engine (Phase 73, D091).

A third engine alongside `engine.py` (D025's fixed SMA) and `engine_v2.py`
(the closed-vocabulary rule engine), and deliberately not a replacement
for either. The two existing engines model a strategy as *a rule that
decides long or flat, evaluated on each bar*. The intraday playbook models
one as *a structural event that opens a risk-defined bracket, in either
direction, flat by the close*. Those are different enough that bending
either existing engine into this shape would break it for its current
callers - so this stands beside them, reusing the cost model and the
session/structure modules rather than duplicating them.

**The replay is strictly causal.** Bars are walked in order and every
input a setup sees is sliced to `[0 .. i]`. Session levels are rebuilt
per session from PRIOR sessions only, swings are filtered by
`confirmed_ts`, and indicators are computed from trailing windows. Nothing
downstream can reach a bar that has not printed.

**The playbook's risk controls are enforced here, not left to the
strategy.** One position at a time; a daily loss limit that stops the
session; a consecutive-loss pause. Those are section 18's rules, and
their whole point is that they bind regardless of how attractive the next
setup looks.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, DivisionByZero, InvalidOperation

from apps.api.app.backtesting.brackets import (
    BracketPlan,
    BracketTrade,
    position_size,
    simulate_bracket,
    stop_quality_ok,
)
from apps.api.app.backtesting.costs import CostModel
from apps.api.app.backtesting.setups import SETUPS, BarContext, SetupSignal
from apps.api.app.marketdata.indicators import InsufficientDataError, atr, ema, rsi
from apps.api.app.marketdata.sessions import (
    build_session_levels,
    group_by_session,
    is_regular_hours,
    session_date,
    session_vwap,
)
from apps.api.app.marketdata.structure import Direction, find_swings

_INDICATOR_LOOKBACK = 80
"""Trailing bars handed to each indicator. Comfortably more than any
period used here (ATR/RSI 14, EMA 21) and bounded so the replay stays
linear rather than re-reading the whole series at every bar."""


@dataclass(frozen=True)
class IntradayRunConfig:
    """Everything that defines a run, so two runs with equal configs are
    the same experiment."""

    symbol: str
    setups: tuple[str, ...] = ("sweep_mss",)
    plan: BracketPlan = field(default_factory=BracketPlan)
    costs: CostModel = field(default_factory=CostModel.frictionless)
    starting_equity: Decimal = Decimal("100000")
    min_score: int = 0
    """Section 14's trade filter. Its own text calls 8/10 a starting test
    value rather than a proven cutoff, so the default admits everything
    and the threshold is varied as an experiment instead of assumed."""

    daily_loss_limit_r: Decimal | None = Decimal("-2")
    """Section 18: stop trading for the session at -1.5R to -2R."""

    max_consecutive_losses: int | None = 3
    """Section 18: pause and reassess after 2-3 consecutive losses. Here
    the pause runs to the end of the session, which is the only
    mechanical reading of "reassess the regime" a backtest can honour."""

    swing_strength: int = 2
    opening_range_minutes: int = 15
    allow_directions: tuple[Direction, ...] = (Direction.LONG, Direction.SHORT)


@dataclass
class IntradayRunResult:
    config: IntradayRunConfig
    trades: list[BracketTrade] = field(default_factory=list)
    sessions_traded: int = 0
    sessions_available: int = 0
    signals_seen: int = 0
    signals_rejected_by_score: int = 0
    signals_rejected_by_risk_controls: int = 0
    signals_rejected_by_sizing: int = 0
    signals_rejected_by_stop_quality: int = 0

    @property
    def equity_curve(self) -> list[tuple[date, Decimal]]:
        """Cumulative realised equity, one point per trade's session."""
        equity = self.config.starting_equity
        points: list[tuple[date, Decimal]] = []
        for trade in self.trades:
            equity += trade.gross_pnl
            points.append((session_date(trade.entry_ts), equity))
        return points


def _indicator(fn: Callable, *args: object, **kwargs: object) -> Decimal | None:
    """`None` when an indicator is not computable yet, never a zero.

    Indicators refuse rather than estimate when a window is short, which is
    correct and happens on every bar of every warmup. Zero would be a real
    value that compares - an RSI of 0 is maximally oversold, so swallowing
    the refusal into a zero would fire a mean-reversion setup on exactly
    the bars where nothing is known.

    **The caught set is narrow on purpose.** An earlier version caught bare
    `Exception`, which in a research engine is worse than no handler at
    all: a genuine bug - a wrong argument, a bad slice, a type error -
    would be silently converted into "no signal", and the run would
    complete with fewer trades and no indication that anything failed.
    Only the refusals an indicator legitimately raises are absorbed.
    """
    try:
        return fn(*args, **kwargs)
    except (InsufficientDataError, InvalidOperation, DivisionByZero, ZeroDivisionError):
        return None


def _bias_timeline(
    higher_bars: Sequence, *, fast: int, slow: int
) -> list[tuple[object, Direction]]:
    """Regime bias at the close of each higher-timeframe bar, computed once.

    Section 3's filter is EMA21 vs EMA50 on the structure timeframe. The
    obvious implementation - recompute it from the full series at every
    execution bar - is quadratic, and on a real run (23,808 five-minute
    bars against 16,104 fifteen-minute ones) it does not finish. Building
    the timeline once and then reading it forward is linear and gives
    identical answers.

    Causality is preserved by what goes IN: each point uses only bars up
    to and including the one that closed, and the lookup below then takes
    the latest point whose bar had already closed.
    """
    closes: list[Decimal] = []
    timeline: list[tuple[object, Direction]] = []
    for bar in sorted(higher_bars, key=lambda b: b.ts):
        closes.append(bar.close)
        if len(closes) < slow + 1:
            continue
        window = closes[-_INDICATOR_LOOKBACK:]
        fast_v = _indicator(ema, window, fast)
        slow_v = _indicator(ema, window, slow)
        if fast_v is None or slow_v is None:
            continue
        timeline.append(
            (bar.ts, Direction.LONG if fast_v > slow_v else Direction.SHORT)
        )
    return timeline


class _BiasLookup:
    """Forward-only reader over a precomputed bias timeline.

    The stamps are extracted ONCE at construction. Rebuilding them inside
    the lookup - the obvious first cut - reintroduces the quadratic cost
    the timeline was built to remove, just one level down, and it is
    invisible because the answers stay correct: the run simply never
    finishes.
    """

    def __init__(self, timeline: list[tuple[object, Direction]]) -> None:
        self._stamps = [ts for ts, _ in timeline]
        self._values = [value for _, value in timeline]

    def at(self, as_of) -> Direction | None:
        """The most recent bias from a higher-timeframe bar that had
        already CLOSED at `as_of`. A bar still in progress is not
        information a lower-timeframe decision can have."""
        import bisect

        if not self._stamps:
            return None
        idx = bisect.bisect_right(self._stamps, as_of) - 1
        return self._values[idx] if idx >= 0 else None


def run_intraday_backtest(
    bars: Sequence,
    config: IntradayRunConfig,
    *,
    higher_timeframe_bars: Sequence | None = None,
    confirm_bars: Sequence | None = None,
) -> IntradayRunResult:
    """Replay `bars` (the execution timeframe) under `config`.

    `higher_timeframe_bars` supplies section 3's regime filter and
    `confirm_bars` the cross-market layer of strategy 5 (QQQ/NDX for
    TQQQ). Both are optional: a setup that wants a layer which is absent
    simply scores no point for it, rather than the run refusing to start.
    """
    result = IntradayRunResult(config=config)
    if not bars:
        return result

    ordered = sorted(bars, key=lambda b: b.ts)
    # Levels for the WHOLE series up front. This is not look-ahead: each
    # session's entry describes its own premarket and opening range plus
    # the PREVIOUS session's extremes, and the engine only ever reads the
    # entry for the session it is currently replaying.
    levels_by_session = build_session_levels(
        ordered, opening_range_minutes=config.opening_range_minutes
    )
    sessions = group_by_session(ordered)
    result.sessions_available = len(sessions)

    higher_timeline = _BiasLookup(
        _bias_timeline(higher_timeframe_bars, fast=21, slow=50)
        if higher_timeframe_bars
        else []
    )
    confirm_timeline = _BiasLookup(
        _bias_timeline(confirm_bars, fast=9, slow=21) if confirm_bars else []
    )

    detectors = [(name, SETUPS[name]) for name in config.setups if name in SETUPS]
    if not detectors:
        raise ValueError(
            f"No known setups selected; got {config.setups!r}, "
            f"known are {sorted(SETUPS)}."
        )

    equity = config.starting_equity

    for day in sorted(sessions):
        session_bars = sessions[day]
        regular = [b for b in session_bars if is_regular_hours(b.ts)]
        if len(regular) < 6:
            continue

        levels = levels_by_session[day]
        vwap_points = {p.ts: p for p in session_vwap(session_bars)}
        # Swings are found on the session's regular bars and each carries
        # its own confirmation time, so `last_confirmed_swing` can hide
        # the ones not yet knowable at any given bar.
        swings = find_swings(regular, strength=config.swing_strength)

        traded_this_session = False
        session_r = Decimal(0)
        consecutive_losses = 0
        blocked = False
        next_free_index = 0

        for i, bar in enumerate(regular):
            if blocked or i < next_free_index:
                continue

            window = regular[max(0, i - _INDICATOR_LOOKBACK) : i + 1]
            closes = [b.close for b in window]

            ctx = BarContext(
                bars=regular[: i + 1],
                index=i,
                levels=levels,
                swings=swings,
                vwap=vwap_points.get(bar.ts),
                atr=_indicator(atr, window, 14),
                rsi=_indicator(rsi, closes, 14),
                ema_fast=_indicator(ema, closes, 9),
                ema_slow=_indicator(ema, closes, 21),
                higher_tf_bias=higher_timeline.at(bar.ts),
                confirm_bias=confirm_timeline.at(bar.ts),
            )

            signal: SetupSignal | None = None
            for _name, detect in detectors:
                signal = detect(ctx)
                if signal is not None:
                    break
            if signal is None:
                continue

            result.signals_seen += 1

            if signal.direction not in config.allow_directions:
                continue
            if signal.score < config.min_score:
                result.signals_rejected_by_score += 1
                continue

            if not stop_quality_ok(
                entry_price=signal.entry_price,
                stop_price=signal.stop_price,
                atr=ctx.atr,
                plan=config.plan,
            ):
                result.signals_rejected_by_stop_quality += 1
                continue

            quantity, capped = position_size(
                equity=equity,
                entry_price=signal.entry_price,
                stop_price=signal.stop_price,
                plan=config.plan,
            )
            if quantity <= 0:
                result.signals_rejected_by_sizing += 1
                continue

            trade = simulate_bracket(
                regular,
                symbol=config.symbol,
                direction=signal.direction,
                entry_index=i,
                entry_price=signal.entry_price,
                stop_price=signal.stop_price,
                quantity=quantity,
                atr=ctx.atr,
                plan=config.plan,
                costs=config.costs,
                setup_name=signal.setup_name,
                size_was_capped=capped,
            )
            if trade is None:
                result.signals_rejected_by_sizing += 1
                continue

            result.trades.append(trade)
            equity += trade.gross_pnl
            session_r += trade.r_multiple
            traded_this_session = True

            consecutive_losses = consecutive_losses + 1 if trade.r_multiple < 0 else 0

            # --- Section 18's controls, enforced by the engine. ---
            if (
                config.daily_loss_limit_r is not None
                and session_r <= config.daily_loss_limit_r
            ):
                blocked = True
                result.signals_rejected_by_risk_controls += 1
            if (
                config.max_consecutive_losses is not None
                and consecutive_losses >= config.max_consecutive_losses
            ):
                blocked = True
                result.signals_rejected_by_risk_controls += 1

            # One position at a time: resume scanning only after this
            # trade has closed. Overlapping entries would compound the
            # per-trade risk into an exposure nobody sized for.
            exit_ts = trade.exit_ts
            next_free_index = i + 1
            if exit_ts is not None:
                for j in range(i + 1, len(regular)):
                    if regular[j].ts > exit_ts:
                        next_free_index = j
                        break
                else:
                    next_free_index = len(regular)

        if traded_this_session:
            result.sessions_traded += 1

    return result
