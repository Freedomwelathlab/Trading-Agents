"""Price-structure detection: swings, liquidity sweeps, market-structure
shifts (Phase 73, D091).

The TQQQ playbook's highest-priority setup is a sequence, not an
indicator reading: *sweep -> reclaim -> displacement -> market structure
shift -> retest -> entry*. Each step is a fact about price relative to a
LOCATION, which is why none of it can be expressed in the existing
`{op, left, right}` rule vocabulary - that compares two indicator series,
and a swept previous-day low is neither.

**The single most important property of this module is that nothing it
reports is knowable before its `confirmed_ts`.**

A swing high is defined by the bars on BOTH sides of it. At the moment
that bar prints, it is not yet a swing high - it becomes one only once
enough bars have followed without exceeding it. Detecting swings by
scanning a completed series and then feeding them to a backtest as though
they were available at the time they occurred is the classic look-ahead
bias, and it does not announce itself: the equity curve simply comes out
better than the strategy could ever have traded. The playbook names
avoiding look-ahead as a requirement, so every structure object here
carries the timestamp at which it became KNOWN, distinct from the
timestamp at which it happened, and callers are expected to gate on the
former.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from apps.api.app.marketdata.ohlcv import OHLCVBar

DEFAULT_SWING_STRENGTH = 2
"""Bars required either side of a pivot. Two is the common intraday
choice: it needs five bars in total, so a swing is confirmed two bars
(ten minutes on a 5-minute chart) after it printed. Larger values find
fewer, more significant swings and confirm them later - a genuine
trade-off, which is why it is a parameter rather than a constant."""


class Direction(str, enum.Enum):  # noqa: UP042 (str mixin matches this codebase)
    """Which way a setup points.

    Named for the trade rather than the price move, because the playbook
    is symmetric and every rule below is written once and mirrored: a
    sell-side sweep (price dips under support and recovers) sets up a
    LONG, and a buy-side sweep sets up a SHORT.
    """

    LONG = "long"
    SHORT = "short"

    @property
    def opposite(self) -> Direction:
        return Direction.SHORT if self is Direction.LONG else Direction.LONG


class SwingKind(str, enum.Enum):  # noqa: UP042
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class SwingPoint:
    """A confirmed pivot.

    `ts` is when the pivot BAR printed. `confirmed_ts` is when it became
    identifiable - `strength` bars later. Any decision made at a time
    between the two is using information that did not exist yet.
    """

    kind: SwingKind
    ts: datetime
    price: Decimal
    confirmed_ts: datetime

    def is_known_at(self, ts: datetime) -> bool:
        return ts >= self.confirmed_ts




def _high(bar: OHLCVBar) -> Decimal:
    return bar.high if bar.high is not None else bar.close


def _low(bar: OHLCVBar) -> Decimal:
    return bar.low if bar.low is not None else bar.close


def find_swings(
    bars: Sequence[OHLCVBar], *, strength: int = DEFAULT_SWING_STRENGTH
) -> list[SwingPoint]:
    """Fractal pivots: a high exceeding `strength` bars on each side.

    Ties are resolved by REJECTING the pivot - a bar only qualifies if it
    is strictly above (or below) its neighbours. On a flat or double-topped
    stretch that means no swing is reported, which is the honest answer:
    two equal highs give no structural information about which one matters,
    and picking the earlier or later one arbitrarily would produce a
    "structure break" that is really a rounding of a tie.
    """
    if strength < 1:
        raise ValueError(f"strength must be at least 1; got {strength}.")
    if len(bars) < 2 * strength + 1:
        return []

    swings: list[SwingPoint] = []
    for i in range(strength, len(bars) - strength):
        window = bars[i - strength : i + strength + 1]
        pivot = bars[i]
        confirmed_ts = bars[i + strength].ts

        highs = [_high(b) for b in window]
        if _high(pivot) == max(highs) and highs.count(_high(pivot)) == 1:
            swings.append(
                SwingPoint(
                    kind=SwingKind.HIGH,
                    ts=pivot.ts,
                    price=_high(pivot),
                    confirmed_ts=confirmed_ts,
                )
            )

        lows = [_low(b) for b in window]
        if _low(pivot) == min(lows) and lows.count(_low(pivot)) == 1:
            swings.append(
                SwingPoint(
                    kind=SwingKind.LOW,
                    ts=pivot.ts,
                    price=_low(pivot),
                    confirmed_ts=confirmed_ts,
                )
            )

    swings.sort(key=lambda s: (s.ts, s.kind.value))
    return swings


def last_confirmed_swing(
    swings: Sequence[SwingPoint], kind: SwingKind, *, as_of: datetime
) -> SwingPoint | None:
    """The most recent swing of `kind` that was already KNOWN at `as_of`.

    The `confirmed_ts` filter is the whole point. Selecting by `ts` alone
    would let a strategy break "the most recent lower high" using a pivot
    that the market had not yet formed - the single easiest way to
    manufacture a profitable backtest by accident.
    """
    candidates = [s for s in swings if s.kind is kind and s.confirmed_ts <= as_of]
    return max(candidates, key=lambda s: s.ts) if candidates else None


@dataclass(frozen=True)
class LiquiditySweep:
    """Price traded through a known level and closed back on the other side.

    This is step one of the playbook's master sequence, and the reason it
    is one object rather than two conditions is that a sweep is only a
    sweep once it has been RECLAIMED. Price trading below support is a
    breakdown; price trading below support and closing back above it is a
    failed breakdown, and only the second is a reversal signal. Reporting
    the first as a sweep is how a mean-reversion system ends up buying
    every leg of a downtrend.
    """

    direction: Direction
    level_name: str
    level_price: Decimal
    extreme_price: Decimal
    swept_ts: datetime
    reclaimed_ts: datetime

    @property
    def penetration(self) -> Decimal:
        """How far beyond the level price reached. The playbook's stop sits
        beyond this, not beyond the level."""
        if self.direction is Direction.LONG:
            return self.level_price - self.extreme_price
        return self.extreme_price - self.level_price


def detect_sweep(
    bars: Sequence[OHLCVBar],
    *,
    level_price: Decimal,
    level_name: str,
    direction: Direction,
    max_bars_to_reclaim: int = 6,
) -> LiquiditySweep | None:
    """The first sweep-and-reclaim of `level_price` within `bars`.

    `max_bars_to_reclaim` bounds how long the excursion may last. Without
    it, price that breaks support, trends down for two hours and then
    ticks back above the old level would register as a "sweep" - the
    mechanical definition is satisfied, but nothing about it is a
    liquidity event. The playbook's own framing is a quick stop-run
    followed by a reclaim, so the window is part of the definition rather
    than a tuning knob bolted on afterwards.

    Scans for a LONG setup below the level and a SHORT setup above it.
    """
    if max_bars_to_reclaim < 1:
        raise ValueError(
            f"max_bars_to_reclaim must be at least 1; got {max_bars_to_reclaim}."
        )

    def _breached(bar: OHLCVBar) -> bool:
        return (
            _low(bar) < level_price
            if direction is Direction.LONG
            else _high(bar) > level_price
        )

    i = 0
    while i < len(bars):
        if not _breached(bars[i]):
            i += 1
            continue

        # One EXCURSION: from the first bar that breaks the level to the
        # first bar that closes back on the other side. Measuring from the
        # first breach is what distinguishes a stop-run from a drift.
        #
        # Re-entering this scan at each bar inside an excursion - rather
        # than stepping over it - would let an eight-bar grind below
        # support report a "sweep" beginning two bars before it recovered,
        # because that tail alone fits the window. The mechanical
        # definition would be satisfied and the setup would be fictional.
        start = i
        extreme = _low(bars[i]) if direction is Direction.LONG else _high(bars[i])
        j = i
        while j < len(bars):
            bar = bars[j]
            extreme = (
                min(extreme, _low(bar))
                if direction is Direction.LONG
                else max(extreme, _high(bar))
            )
            reclaimed = (
                bar.close > level_price
                if direction is Direction.LONG
                else bar.close < level_price
            )
            if reclaimed:
                if j - start <= max_bars_to_reclaim:
                    return LiquiditySweep(
                        direction=direction,
                        level_name=level_name,
                        level_price=level_price,
                        extreme_price=extreme,
                        swept_ts=bars[start].ts,
                        reclaimed_ts=bar.ts,
                    )
                break  # Reclaimed, but too slowly to be a liquidity event.
            j += 1

        # Resume AFTER this excursion, never inside it.
        i = j + 1

    return None


@dataclass(frozen=True)
class StructureShift:
    """A market-structure shift: the break that confirms a reversal.

    For a long, price closing above the most recent confirmed LOWER HIGH
    says the sequence of lower highs that defined the downtrend has ended.
    The playbook is explicit that this - not the sweep and not a divergence
    - is the confirmation, and that a sweep without one is a stand-aside.
    """

    direction: Direction
    broken_swing: SwingPoint
    break_ts: datetime
    break_price: Decimal


def detect_structure_shift(
    bars: Sequence[OHLCVBar],
    swings: Sequence[SwingPoint],
    *,
    direction: Direction,
    after_ts: datetime,
) -> StructureShift | None:
    """The first close through the relevant swing, strictly after `after_ts`.

    The swing being broken must have been confirmed at or before the bar
    that breaks it - enforced through `last_confirmed_swing`, evaluated per
    bar rather than once up front. Evaluating it once would pick the swing
    known at the END of the series and quietly back-date it.

    A CLOSE through the level is required, not a touch. Intrabar
    penetration that closes back is exactly the failed break this whole
    module exists to distinguish from a real one.
    """
    kind = SwingKind.HIGH if direction is Direction.LONG else SwingKind.LOW

    for bar in bars:
        if bar.ts <= after_ts:
            continue
        swing = last_confirmed_swing(swings, kind, as_of=bar.ts)
        if swing is None or swing.ts > bar.ts:
            continue
        broke = (
            bar.close > swing.price
            if direction is Direction.LONG
            else bar.close < swing.price
        )
        if broke:
            return StructureShift(
                direction=direction,
                broken_swing=swing,
                break_ts=bar.ts,
                break_price=bar.close,
            )
    return None
