"""Chart signals over the WHOLE trading day, with a measured confidence
(Phase 98, D117).

Two things the Markets chart got wrong, both fixed here.

**Half the day was never scanned.** The chart endpoint replayed the
detectors over regular-hours bars only, while the chart itself draws
pre-market and after-hours bars - so roughly half of every day on screen
could not carry a marker, whatever the tape did. The bot's `auto` mode has
scanned the whole extended day since Phase 88; the chart now does the same
by default (`market_type="auto"`), with `regular` kept for the backtest's
own view.

**A score is not a probability.** Setup scores are points of evidence
(1-6 in practice; nothing has scored 7 on TQQQ), and the operator asked
for "at least 70% confident of a winning trade". That is a claim about
outcomes, so it is MEASURED rather than asserted: every signal in the
calibration window is followed forward bar by bar, and it is a win only if
price reaches one risk-unit in its favour (entry +/- |entry - stop|)
BEFORE it touches the stop, within the same session. A bar that spans both
counts as a loss, because a 5-minute bar cannot say which came first.
Signals still open at the session's end are neither and are left out. The
confidence of a signal is then the win rate of its own bucket - same
setup, same direction, same score - and only when that bucket holds at
least `MIN_SAMPLE` resolved signals; below that it is None, never a
number from three trades.

This is an in-sample measurement over the stored history, and says so. It
tells an operator how often this exact kind of signal has worked here; it
is not a forecast, and a bucket at 70% on 12 trades is weak evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from apps.api.app.autotrade.scanner import MIN_SESSION_BARS, _indicator
from apps.api.app.backtesting.setups import SETUPS, BarContext, SetupSignal
from apps.api.app.marketdata.indicators import atr, ema, rsi
from apps.api.app.marketdata.sessions import session_phase
from apps.api.app.marketdata.structure import Direction, find_swings

MarketType = Literal["auto", "regular"]

MIN_SAMPLE = 10
"""Resolved signals a bucket needs before its win rate is reported."""

INDICATOR_LOOKBACK = 80


@dataclass(frozen=True)
class ScannedSignal:
    signal: SetupSignal
    ts: Any
    phase: str
    outcome: Literal["win", "loss", "open"]


def _phase_bars(day_bars: Sequence[Any], market_type: MarketType) -> list[Any]:
    from apps.api.app.marketdata.sessions import SessionPhase

    if market_type == "regular":
        return [b for b in day_bars if session_phase(b.ts) is SessionPhase.REGULAR]
    return [b for b in day_bars if session_phase(b.ts) is not None]


def _outcome(bars: Sequence[Any], i: int, signal: SetupSignal) -> Literal["win", "loss", "open"]:
    entry = signal.entry_price
    stop = signal.stop_price
    risk = abs(entry - stop)
    if risk <= 0:
        return "open"
    long = signal.direction is Direction.LONG
    target = entry + risk if long else entry - risk
    for bar in bars[i + 1 :]:
        hit_stop = bar.low <= stop if long else bar.high >= stop
        hit_target = bar.high >= target if long else bar.low <= target
        if hit_stop:
            return "loss"  # includes the ambiguous both-in-one-bar case
        if hit_target:
            return "win"
    return "open"


def scan_signals(
    bars: Sequence[Any],
    *,
    names: Sequence[str],
    sessions: int,
    market_type: MarketType,
    levels_by_day: dict[Any, Any],
    by_day: dict[Any, list[Any]],
    vwap_for_day: Any,
) -> tuple[list[ScannedSignal], int]:
    """Replay the detectors over the last `sessions` sessions, the way the
    bot's scanner builds its context (Phase 88), and follow each signal
    forward to its outcome. Returns (signals, bars scanned)."""
    out: list[ScannedSignal] = []
    scanned = 0
    for day in sorted(by_day)[-sessions:]:
        day_bars = by_day[day]
        phase_bars = _phase_bars(day_bars, market_type)
        levels = levels_by_day.get(day)
        if levels is None or len(phase_bars) < MIN_SESSION_BARS:
            continue
        vwap_points = vwap_for_day(day_bars)
        swings = find_swings(phase_bars, strength=3)
        scanned += len(phase_bars)
        for i in range(len(phase_bars)):
            window = phase_bars[max(0, i - INDICATOR_LOOKBACK) : i + 1]
            closes = [b.close for b in window]
            ctx = BarContext(
                bars=phase_bars[: i + 1],
                index=i,
                levels=levels,
                swings=swings,
                vwap=vwap_points.get(phase_bars[i].ts),
                atr=_indicator(atr, window, 14),
                rsi=_indicator(rsi, closes, 14),
                ema_fast=_indicator(ema, closes, 9),
                ema_slow=_indicator(ema, closes, 21),
            )
            phase = session_phase(phase_bars[i].ts)
            for name in names:
                signal = SETUPS[name](ctx)
                if signal is None:
                    continue
                out.append(
                    ScannedSignal(
                        signal=signal,
                        ts=phase_bars[i].ts,
                        phase=phase.value if phase else "closed",
                        outcome=_outcome(phase_bars, i, signal),
                    )
                )
    return out, scanned


BucketKey = tuple[str, str, int]


@dataclass(frozen=True)
class Bucket:
    resolved: int
    wins: int

    @property
    def win_rate(self) -> Decimal | None:
        if self.resolved < MIN_SAMPLE:
            return None
        return (Decimal(self.wins) * 100 / Decimal(self.resolved)).quantize(Decimal("0.1"))


def bucket_key(signal: SetupSignal) -> BucketKey:
    return (signal.setup_name, signal.direction.value, signal.score)


def calibrate(signals: Sequence[ScannedSignal]) -> dict[BucketKey, Bucket]:
    counts: dict[BucketKey, list[int]] = {}
    for s in signals:
        if s.outcome == "open":
            continue
        c = counts.setdefault(bucket_key(s.signal), [0, 0])
        c[0] += 1
        c[1] += 1 if s.outcome == "win" else 0
    return {k: Bucket(resolved=v[0], wins=v[1]) for k, v in counts.items()}
