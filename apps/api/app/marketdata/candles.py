"""Candlestick pattern detection (Phase 76, D094).

Encodes the candlestick patterns taught in the ASTA course materials'
SMM (Smart Money) and PAPA (Price Action) modules, using THEIR exact
definitions rather than a generic library's. The material is specific in
ways that matter, and those specifics are preserved here:

  * a Hammer's lower wick is "2-3 times the body";
  * Bullish Piercing "closes above the median of the first candle" but not
    above its open (that would be an engulf);
  * Bullish Engulf "closes above the open of the first bearish candle";
  * a reversal candle is "significant only at the end of a trend — ignore
    it in the middle of a trend or range."

That last rule is why every reversal pattern here reports the trend context
it REQUIRES rather than asserting a signal on shape alone. A hammer shape in
the middle of a range is not a hammer signal; the caller supplies the trend
(from `structure.py`) and `is_signal_in_context` applies the rule. Detecting
the shape and confirming the context are kept separate so neither silently
does the other's job.

Nothing here is TQQQ- or instrument-specific. Bars are read through the
shared `OHLCVBar` protocol, so the same detectors run on any series.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from apps.api.app.marketdata.ohlcv import OHLCVBar


class Trend(str, enum.Enum):  # noqa: UP042 (str mixin matches this codebase)
    UP = "up"
    DOWN = "down"
    SIDEWAYS = "sideways"


class PatternDirection(str, enum.Enum):  # noqa: UP042
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class CandlePattern:
    """A detected pattern anchored to the bar that COMPLETES it.

    `requires_trend` is the context the material demands for the shape to be
    a signal — `Trend.DOWN` for a bullish reversal, `Trend.UP` for a bearish
    one, `None` for a continuation/indecision pattern that needs no prior
    trend. `is_signal_in_context` is the one place that rule is applied, so a
    caller cannot accidentally treat a mid-trend hammer as a buy.
    """

    name: str
    direction: PatternDirection
    completed_index: int
    requires_trend: Trend | None

    def is_signal_in_context(self, prior_trend: Trend) -> bool:
        if self.requires_trend is None:
            return True
        return prior_trend is self.requires_trend


# --- candle anatomy (PAPA Concept 1: Open/High/Low/Close) -----------------


def _o(b: OHLCVBar) -> Decimal:
    if b.open is None:
        raise ValueError("candlestick patterns need an open price; got a close-only bar.")
    return b.open


def _h(b: OHLCVBar) -> Decimal:
    return b.high if b.high is not None else b.close


def _l(b: OHLCVBar) -> Decimal:
    return b.low if b.low is not None else b.close


def _body(b: OHLCVBar) -> Decimal:
    return abs(b.close - _o(b))


def _range(b: OHLCVBar) -> Decimal:
    return _h(b) - _l(b)


def _upper_wick(b: OHLCVBar) -> Decimal:
    return _h(b) - max(_o(b), b.close)


def _lower_wick(b: OHLCVBar) -> Decimal:
    return min(_o(b), b.close) - _l(b)


def _median(b: OHLCVBar) -> Decimal:
    """Midpoint of the BODY, which is what the material means by a candle's
    'median' when it says piercing 'closes above the median'."""
    return (_o(b) + b.close) / 2


def is_bullish(b: OHLCVBar) -> bool:
    return b.close > _o(b)


def is_bearish(b: OHLCVBar) -> bool:
    return b.close < _o(b)


# --- single-candle patterns -----------------------------------------------

_DOJI_BODY_FRACTION = Decimal("0.1")
"""A doji's body is a small fraction of its range — open and close nearly
equal. 10% of range is the conventional threshold the material's 'open ~
close' description implies."""

_WICK_BODY_MULTIPLE = Decimal("2")
"""Hammer / shooting-star wick is '2-3 times the body' (SMM Concept 1).
Two is the lower bound of that range, used as the qualifying threshold."""


def is_doji(b: OHLCVBar) -> bool:
    r = _range(b)
    if r <= 0:
        return False
    return _body(b) <= _DOJI_BODY_FRACTION * r


def is_hammer(b: OHLCVBar) -> bool:
    """Small body near the top of the range, lower wick >= 2x body, little
    upper wick. A bullish REVERSAL only at the end of a downtrend — the
    shape alone is checked here; context is `CandlePattern.requires_trend`.
    """
    body = _body(b)
    if body <= 0:
        return False
    return _lower_wick(b) >= _WICK_BODY_MULTIPLE * body and _upper_wick(b) <= body


def is_shooting_star(b: OHLCVBar) -> bool:
    """Mirror of the hammer: small body near the low, upper wick >= 2x body.
    Called an Inverted Hammer at a bottom and a Shooting Star at a top; the
    material names the top case the bearish signal, which is the one keyed
    here via `requires_trend=UP`."""
    body = _body(b)
    if body <= 0:
        return False
    return _upper_wick(b) >= _WICK_BODY_MULTIPLE * body and _lower_wick(b) <= body


# --- two-candle patterns --------------------------------------------------


def is_bullish_engulf(prev: OHLCVBar, cur: OHLCVBar) -> bool:
    """First a normal bearish candle, then a bullish candle that closes
    ABOVE the open of the first (engulfing its body). PAPA/SMM Concept."""
    return (
        is_bearish(prev)
        and is_bullish(cur)
        and cur.close > _o(prev)
        and _o(cur) <= prev.close
    )


def is_bearish_engulf(prev: OHLCVBar, cur: OHLCVBar) -> bool:
    return (
        is_bullish(prev)
        and is_bearish(cur)
        and cur.close < _o(prev)
        and _o(cur) >= prev.close
    )


def is_bullish_piercing(prev: OHLCVBar, cur: OHLCVBar) -> bool:
    """Bullish candle closes ABOVE the median of the prior bearish candle's
    body, but NOT above its open — above the open would be a full engulf,
    which the material treats as the stronger, separate pattern."""
    return (
        is_bearish(prev)
        and is_bullish(cur)
        and cur.close > _median(prev)
        and cur.close < _o(prev)
    )


def is_bearish_piercing(prev: OHLCVBar, cur: OHLCVBar) -> bool:
    return (
        is_bullish(prev)
        and is_bearish(cur)
        and cur.close < _median(prev)
        and cur.close > _o(prev)
    )


def is_tweezer_bottom(
    prev: OHLCVBar, cur: OHLCVBar, *, tolerance: Decimal = Decimal("0.001")
) -> bool:
    """Two candles whose LOWS sit at almost the same level — a reversal tell
    at a bottom (PAPA Concept 2). Equality is within a relative tolerance
    because two floats-from-a-vendor rarely match to the cent."""
    lo1, lo2 = _l(prev), _l(cur)
    ref = max(abs(lo1), abs(lo2), Decimal("0.01"))
    return abs(lo1 - lo2) / ref <= tolerance


def is_tweezer_top(
    prev: OHLCVBar, cur: OHLCVBar, *, tolerance: Decimal = Decimal("0.001")
) -> bool:
    hi1, hi2 = _h(prev), _h(cur)
    ref = max(abs(hi1), abs(hi2), Decimal("0.01"))
    return abs(hi1 - hi2) / ref <= tolerance


# --- three-candle patterns ------------------------------------------------


def is_morning_star(a: OHLCVBar, b: OHLCVBar, c: OHLCVBar) -> bool:
    """Bearish candle, then a small/neutral candle (the 'star', gapping down
    in the ideal case), then a bullish candle that closes above the median
    of the FIRST candle. The star's colour does not matter (material)."""
    return (
        is_bearish(a)
        and _body(b) <= _body(a) / 2
        and is_bullish(c)
        and c.close > _median(a)
    )


def is_evening_star(a: OHLCVBar, b: OHLCVBar, c: OHLCVBar) -> bool:
    return (
        is_bullish(a)
        and _body(b) <= _body(a) / 2
        and is_bearish(c)
        and c.close < _median(a)
    )


def is_three_white_soldiers(a: OHLCVBar, b: OHLCVBar, c: OHLCVBar) -> bool:
    """Three consecutive bullish candles, each closing higher than the last."""
    return (
        is_bullish(a)
        and is_bullish(b)
        and is_bullish(c)
        and b.close > a.close
        and c.close > b.close
    )


def is_three_black_crows(a: OHLCVBar, b: OHLCVBar, c: OHLCVBar) -> bool:
    return (
        is_bearish(a)
        and is_bearish(b)
        and is_bearish(c)
        and b.close < a.close
        and c.close < b.close
    )


# --- scan ------------------------------------------------------------------

# (name, direction, requires_trend) for each detector arity.
_ONE: list[tuple[str, Callable[[OHLCVBar], bool], PatternDirection, Trend | None]] = [
    ("hammer", is_hammer, PatternDirection.BULLISH, Trend.DOWN),
    ("shooting_star", is_shooting_star, PatternDirection.BEARISH, Trend.UP),
    ("doji", is_doji, PatternDirection.NEUTRAL, None),
]
_TWO: list[tuple[str, Callable[[OHLCVBar, OHLCVBar], bool], PatternDirection, Trend | None]] = [
    ("bullish_engulf", is_bullish_engulf, PatternDirection.BULLISH, Trend.DOWN),
    ("bearish_engulf", is_bearish_engulf, PatternDirection.BEARISH, Trend.UP),
    ("bullish_piercing", is_bullish_piercing, PatternDirection.BULLISH, Trend.DOWN),
    ("bearish_piercing", is_bearish_piercing, PatternDirection.BEARISH, Trend.UP),
    ("tweezer_bottom", is_tweezer_bottom, PatternDirection.BULLISH, Trend.DOWN),
    ("tweezer_top", is_tweezer_top, PatternDirection.BEARISH, Trend.UP),
]
_ThreeFn = Callable[[OHLCVBar, OHLCVBar, OHLCVBar], bool]
_THREE: list[tuple[str, _ThreeFn, PatternDirection, Trend | None]] = [
    ("morning_star", is_morning_star, PatternDirection.BULLISH, Trend.DOWN),
    ("evening_star", is_evening_star, PatternDirection.BEARISH, Trend.UP),
    ("three_white_soldiers", is_three_white_soldiers, PatternDirection.BULLISH, Trend.DOWN),
    ("three_black_crows", is_three_black_crows, PatternDirection.BEARISH, Trend.UP),
]


def detect_at(bars: Sequence[OHLCVBar], index: int) -> list[CandlePattern]:
    """Every pattern that COMPLETES on `bars[index]`.

    Only backward-looking windows are read (index and the one/two bars
    before it), so this is safe to call bar-by-bar in a causal replay — a
    pattern is reported exactly when its last candle closes, never before.
    """
    found: list[CandlePattern] = []
    cur = bars[index]

    for name1, fn1, direction1, trend1 in _ONE:
        if fn1(cur):
            found.append(CandlePattern(name1, direction1, index, trend1))

    if index >= 1:
        prev = bars[index - 1]
        for name2, fn2, direction2, trend2 in _TWO:
            if fn2(prev, cur):
                found.append(CandlePattern(name2, direction2, index, trend2))

    if index >= 2:
        a, b = bars[index - 2], bars[index - 1]
        for name3, fn3, direction3, trend3 in _THREE:
            if fn3(a, b, cur):
                found.append(CandlePattern(name3, direction3, index, trend3))

    return found
