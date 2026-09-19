"""Scan stored bars for setups at the LATEST bar (Phase 81, D098).

This is the live counterpart of one iteration of
`backtesting/intraday_engine.run_intraday_backtest`. It builds the same
`BarContext` — same session levels, same swings, same VWAP, same
indicator windows — and asks the same detectors. The point of reusing the
construction verbatim rather than re-implementing it is that the live bot
can then never fire on a signal the backtest would not have fired on, and
every measured (negative, see D095) result about these setups applies
unchanged to what the bot does.

Only the last bar is evaluated: a bot cycle asks "is there a setup NOW",
not "replay the day". Every earlier bar is context.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation

from apps.api.app.backtesting.brackets import BracketPlan, stop_quality_ok
from apps.api.app.backtesting.setups import SETUPS, BarContext, SetupSignal
from apps.api.app.marketdata.indicators import InsufficientDataError, atr, ema, rsi
from apps.api.app.marketdata.sessions import (
    SessionPhase,
    build_session_levels,
    group_by_session,
    session_date,
    session_phase,
    session_vwap,
)
from apps.api.app.marketdata.structure import Direction, find_swings

_INDICATOR_LOOKBACK = 80
"""Same trailing window the backtest engine hands each indicator."""

MIN_SESSION_BARS = 6
"""Same floor the backtest engine applies before it will read a session."""


def _indicator(fn: Callable, *args: object, **kwargs: object) -> Decimal | None:
    try:
        return fn(*args, **kwargs)
    except (InsufficientDataError, InvalidOperation, DivisionByZero, ZeroDivisionError):
        return None


@dataclass(frozen=True)
class ScanHit:
    symbol: str
    signal: SetupSignal
    atr: Decimal | None
    last_close: Decimal
    last_ts: object  # datetime; typed loosely to accept any OHLCVBar shape
    phase: SessionPhase


@dataclass(frozen=True)
class ScanOutcome:
    hit: ScanHit | None
    reason: str
    """Why there is no hit, or 'signal' when there is one — every scan
    explains itself so a run row can say "scanned 5, 0 signals because …"."""


def _phase_allowed(phase: SessionPhase | None, market_type: str) -> bool:
    if phase is None:
        return False
    if market_type == "auto":
        return True
    return {
        "regular": SessionPhase.REGULAR,
        "pre_market": SessionPhase.PRE_MARKET,
        "post_market": SessionPhase.AFTER_HOURS,
    }.get(market_type) is phase


def scan_latest_bar(
    symbol: str,
    bars: Sequence,
    *,
    setups: Sequence[str],
    market_type: str,
    min_score: int,
    plan: BracketPlan,
    swing_strength: int = 3,
    allow_directions: Sequence[Direction] = (Direction.LONG,),
) -> ScanOutcome:
    """Evaluate `setups` at the last bar of `bars` and return the best hit.

    `bars` are the symbol's stored bars, oldest first, spanning at least
    the current session and the previous one (levels need the previous
    session's extremes). Bars from other session phases than the bot's
    `market_type` are still used as context — the playbook prices
    premarket highs and lows — but a signal is only reported when the
    LAST bar sits in an allowed phase.

    Long-only by default: the paper broker cannot hold a short, and a bot
    that recorded short signals it could not act on would inflate its own
    "signals found" figure with trades that never existed.
    """
    if not bars:
        return ScanOutcome(None, "no_bars")
    ordered = sorted(bars, key=lambda b: b.ts)
    last = ordered[-1]
    phase = session_phase(last.ts)
    if not _phase_allowed(phase, market_type):
        return ScanOutcome(None, f"phase_{phase.value if phase else 'closed'}_not_allowed")

    detectors = [(name, SETUPS[name]) for name in setups if name in SETUPS]
    if not detectors:
        return ScanOutcome(None, "no_known_setups")

    levels_by_session = build_session_levels(ordered)
    sessions = group_by_session(ordered)
    day = session_date(last.ts)
    session_bars = sessions.get(day, [])
    # The bot evaluates on the phase it is allowed to trade in. For the
    # regular session that is exactly the backtest's `regular` list; for
    # `auto` it is the whole extended day, which the backtest never
    # traded — a documented difference, not a hidden one.
    if market_type == "regular":
        phase_bars = [b for b in session_bars if session_phase(b.ts) is SessionPhase.REGULAR]
    else:
        phase_bars = [b for b in session_bars if session_phase(b.ts) is not None]
    if len(phase_bars) < MIN_SESSION_BARS:
        return ScanOutcome(None, f"only_{len(phase_bars)}_bars_in_phase")
    if phase_bars[-1].ts != last.ts:
        return ScanOutcome(None, "last_bar_outside_phase")

    levels = levels_by_session.get(day)
    if levels is None:
        return ScanOutcome(None, "no_session_levels")
    vwap_points = {p.ts: p for p in session_vwap(session_bars)}
    swings = find_swings(phase_bars, strength=swing_strength)

    i = len(phase_bars) - 1
    window = phase_bars[max(0, i - _INDICATOR_LOOKBACK) : i + 1]
    closes = [b.close for b in window]
    ctx = BarContext(
        bars=phase_bars,
        index=i,
        levels=levels,
        swings=swings,
        vwap=vwap_points.get(last.ts),
        atr=_indicator(atr, window, 14),
        rsi=_indicator(rsi, closes, 14),
        ema_fast=_indicator(ema, closes, 9),
        ema_slow=_indicator(ema, closes, 21),
    )

    best: SetupSignal | None = None
    rejected: list[str] = []
    for name, detect in detectors:
        signal = detect(ctx)
        if signal is None:
            continue
        if signal.direction not in allow_directions:
            rejected.append(f"{name}:direction_{signal.direction.value}")
            continue
        if signal.score < min_score:
            rejected.append(f"{name}:score_{signal.score}<{min_score}")
            continue
        if not stop_quality_ok(
            entry_price=signal.entry_price, stop_price=signal.stop_price, atr=ctx.atr, plan=plan
        ):
            rejected.append(f"{name}:stop_quality")
            continue
        if best is None or signal.score > best.score:
            best = signal

    if best is None:
        return ScanOutcome(None, "no_signal" if not rejected else "rejected:" + ",".join(rejected))
    assert phase is not None
    return ScanOutcome(
        ScanHit(
            symbol=symbol,
            signal=best,
            atr=ctx.atr,
            last_close=last.close,
            last_ts=last.ts,
            phase=phase,
        ),
        "signal",
    )
