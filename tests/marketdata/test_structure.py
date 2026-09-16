"""Structure-detection tests (Phase 73, D091).

The look-ahead tests are the important ones. A swing high is defined by
the bars after it, so code that treats it as available when it printed
produces a backtest that is better than the strategy could have traded -
silently, with no error and a perfectly plausible equity curve.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.api.app.marketdata.structure import (
    Direction,
    SwingKind,
    detect_structure_shift,
    detect_sweep,
    find_swings,
    last_confirmed_swing,
)

T0 = datetime(2026, 6, 17, 13, 30, tzinfo=UTC)


class Bar:
    def __init__(self, i, *, high, low, close=None, open=None):
        self.ts = T0 + timedelta(minutes=5 * i)
        self.open = Decimal(str(open)) if open is not None else None
        self.high = Decimal(str(high))
        self.low = Decimal(str(low))
        self.close = Decimal(str(close if close is not None else (high + low) / 2))
        self.volume = 1_000


def series(spec):
    """spec: list of (high, low) or (high, low, close)."""
    return [Bar(i, high=s[0], low=s[1], close=s[2] if len(s) > 2 else None)
            for i, s in enumerate(spec)]


# --------------------------------------------------------------------------
# Swings and look-ahead


def test_a_swing_is_not_confirmed_until_strength_bars_have_followed():
    """The core anti-look-ahead property. The pivot at index 2 prints at
    13:40, but with strength=2 it cannot be identified until the bar at
    index 4 (13:50) has closed without exceeding it."""
    bars = series([(10, 9), (11, 10), (15, 14), (11, 10), (10, 9)])

    swings = find_swings(bars, strength=2)
    high = next(s for s in swings if s.kind is SwingKind.HIGH)

    assert high.ts == T0 + timedelta(minutes=10)
    assert high.confirmed_ts == T0 + timedelta(minutes=20)
    assert high.confirmed_ts > high.ts

    assert not high.is_known_at(high.ts)
    assert not high.is_known_at(high.confirmed_ts - timedelta(minutes=5))
    assert high.is_known_at(high.confirmed_ts)


def test_last_confirmed_swing_hides_pivots_that_were_not_yet_known():
    """Selecting the most recent swing by `ts` alone would hand a strategy
    a pivot the market had not formed. This is the guard that makes every
    downstream structure break honest."""
    bars = series([(10, 9), (11, 10), (15, 14), (11, 10), (10, 9)])
    swings = find_swings(bars, strength=2)
    high = next(s for s in swings if s.kind is SwingKind.HIGH)

    assert last_confirmed_swing(swings, SwingKind.HIGH, as_of=high.ts) is None
    assert last_confirmed_swing(swings, SwingKind.HIGH, as_of=high.confirmed_ts) == high


def test_a_tied_high_is_not_a_swing():
    """Two equal highs give no information about which one matters.
    Reporting either would create a "structure break" that is really a
    tie-break, so neither is reported."""
    bars = series([(10, 9), (15, 10), (14, 13), (15, 10), (10, 9)])

    highs = [s for s in find_swings(bars, strength=2) if s.kind is SwingKind.HIGH]

    assert highs == []


def test_too_few_bars_yields_no_swings_rather_than_a_partial_pivot():
    assert find_swings(series([(10, 9), (11, 10)]), strength=2) == []


def test_strength_must_be_at_least_one():
    with pytest.raises(ValueError, match="at least 1"):
        find_swings(series([(1, 1)]), strength=0)


def test_larger_strength_confirms_later():
    """The trade-off the parameter exists to express: more significant
    pivots, known later."""
    bars = series([(10, 9), (11, 10), (12, 11), (20, 19), (12, 11), (11, 10), (10, 9)])

    weak = next(s for s in find_swings(bars, strength=1) if s.kind is SwingKind.HIGH)
    strong = next(s for s in find_swings(bars, strength=3) if s.kind is SwingKind.HIGH)

    assert weak.ts == strong.ts
    assert strong.confirmed_ts > weak.confirmed_ts


# --------------------------------------------------------------------------
# Liquidity sweeps


def test_a_sweep_requires_a_reclaim_not_just_a_breach():
    """Price below support is a breakdown; price below support that closes
    back above is a failed breakdown. Only the second is a reversal setup -
    treating the first as one is how a system buys every leg down."""
    breaks_and_keeps_falling = series([(10, 9, 9.5), (9, 8, 8.2), (8, 7, 7.1), (7, 6, 6.2)])

    swept = detect_sweep(
        breaks_and_keeps_falling,
        level_price=Decimal("9"),
        level_name="PDL",
        direction=Direction.LONG,
    )

    assert swept is None


def test_a_sweep_is_reported_when_price_reclaims_the_level():
    bars = series([(10, 9.5, 9.8), (9.6, 8.5, 8.7), (9.4, 8.8, 9.3)])

    sweep = detect_sweep(
        bars, level_price=Decimal("9"), level_name="PDL", direction=Direction.LONG
    )

    assert sweep is not None
    assert sweep.level_name == "PDL"
    assert sweep.extreme_price == Decimal("8.5")
    assert sweep.swept_ts == T0 + timedelta(minutes=5)
    assert sweep.reclaimed_ts == T0 + timedelta(minutes=10)
    assert sweep.penetration == Decimal("0.5")


def test_a_short_sweep_mirrors_the_long_one():
    """The playbook is symmetric, and so is this - a buy-side sweep above
    resistance that closes back below sets up a short."""
    bars = series([(9.5, 9, 9.2), (11.5, 10, 11.2), (11.2, 9.5, 9.7)])

    sweep = detect_sweep(
        bars, level_price=Decimal("11"), level_name="PDH", direction=Direction.SHORT
    )

    assert sweep is not None
    assert sweep.direction is Direction.SHORT
    assert sweep.extreme_price == Decimal("11.5")
    assert sweep.penetration == Decimal("0.5")


def test_a_reclaim_that_takes_too_long_is_not_a_sweep():
    """A slow grind below the level that eventually recovers satisfies the
    mechanical definition but is not a liquidity event. The window is part
    of the definition, not a tuning knob."""
    bars = series(
        [(10, 9.5, 9.8), (9.4, 8.5, 8.6)]
        + [(9.0, 8.4, 8.6)] * 8
        + [(9.5, 8.9, 9.4)]
    )

    assert (
        detect_sweep(
            bars,
            level_price=Decimal("9"),
            level_name="PDL",
            direction=Direction.LONG,
            max_bars_to_reclaim=3,
        )
        is None
    )
    assert (
        detect_sweep(
            bars,
            level_price=Decimal("9"),
            level_name="PDL",
            direction=Direction.LONG,
            max_bars_to_reclaim=20,
        )
        is not None
    )


def test_sweep_window_must_be_positive():
    with pytest.raises(ValueError, match="at least 1"):
        detect_sweep(
            series([(1, 1)]),
            level_price=Decimal("1"),
            level_name="x",
            direction=Direction.LONG,
            max_bars_to_reclaim=0,
        )


# --------------------------------------------------------------------------
# Market-structure shift


def test_a_structure_shift_needs_a_close_through_the_swing_not_a_touch():
    """Intrabar penetration that closes back inside is the failed break
    this module exists to distinguish from a real one."""
    bars = series(
        [
            (10, 9), (11, 10), (14, 13), (11, 10), (10, 9),   # swing high at idx 2 = 14
            (14.5, 10, 13.0),                                  # pokes above, closes below
        ]
    )
    swings = find_swings(bars, strength=2)

    shift = detect_structure_shift(
        bars, swings, direction=Direction.LONG, after_ts=T0
    )

    assert shift is None


def test_a_close_above_the_confirmed_lower_high_is_a_bullish_shift():
    bars = series(
        [
            (10, 9), (11, 10), (14, 13), (11, 10), (10, 9),
            (15, 10, 14.5),
        ]
    )
    swings = find_swings(bars, strength=2)

    shift = detect_structure_shift(bars, swings, direction=Direction.LONG, after_ts=T0)

    assert shift is not None
    assert shift.broken_swing.price == Decimal("14")
    assert shift.break_price == Decimal("14.5")
    assert shift.break_ts == T0 + timedelta(minutes=25)


def test_no_bar_can_close_through_a_swing_before_it_is_confirmed():
    """Why the confirmation guard rarely fires - and must stay anyway.

    The guard cannot be demonstrated by a bar that breaks a pivot during
    its confirmation window, because no such bar exists: a bar closing
    above a pivot's high also EXCEEDS that high, which disqualifies the
    pivot. The invariant is structural, not defensive.

    Asserted as a property over the detector's own output rather than
    stated in a comment, so a future change to the pivot rule that breaks
    it - a `>=` comparison, say, or a window that stops including the
    pivot's neighbours - fails here instead of silently admitting
    look-ahead.
    """
    bars = series(
        [(10, 9), (11, 10), (14, 13), (11, 10), (10, 9), (15, 10, 14.5),
         (11, 10), (9, 8), (12, 11), (10, 9), (8, 7), (13, 6, 12.5)]
    )

    for swing in find_swings(bars, strength=2):
        during_window = [
            b for b in bars if swing.ts < b.ts < swing.confirmed_ts
        ]
        for bar in during_window:
            if swing.kind is SwingKind.HIGH:
                assert bar.close <= swing.price
            else:
                assert bar.close >= swing.price


def test_the_shift_uses_the_swing_confirmed_at_that_bar_not_the_last_one():
    """Where look-ahead genuinely could enter: swing SELECTION.

    Here a high of 14 forms first, and a LOWER high of 12 forms later.
    Breaking "the most recent lower high" means 12 once that pivot is
    confirmed, and 14 before it. An implementation that resolved the
    relevant swing once, from the finished series, would use whichever
    pivot happened to be last overall and back-date it across the whole
    replay.
    """
    bars = series(
        [
            (10, 9), (11, 10), (14, 13), (11, 10), (10, 9),      # high 14 @ idx2
            (10, 9), (12, 11), (10, 9), (9, 8),                   # high 12 @ idx6
            (13, 8, 12.5),                                        # breaks 12, not 14
        ]
    )
    swings = find_swings(bars, strength=2)

    shift = detect_structure_shift(bars, swings, direction=Direction.LONG, after_ts=T0)

    assert shift is not None
    assert shift.broken_swing.price == Decimal("12")
    assert shift.break_price == Decimal("12.5")
    assert shift.broken_swing.confirmed_ts <= shift.break_ts


def test_a_bearish_shift_breaks_a_higher_low_downward():
    bars = series(
        [
            (11, 10), (10, 9), (7, 6), (10, 9), (11, 10),
            (10, 5, 5.5),
        ]
    )
    swings = find_swings(bars, strength=2)

    shift = detect_structure_shift(bars, swings, direction=Direction.SHORT, after_ts=T0)

    assert shift is not None
    assert shift.direction is Direction.SHORT
    assert shift.broken_swing.kind is SwingKind.LOW
    assert shift.broken_swing.price == Decimal("6")


def test_only_bars_after_the_reference_time_are_considered():
    """A shift must follow the sweep that set it up, not precede it."""
    bars = series(
        [
            (10, 9), (11, 10), (14, 13), (11, 10), (10, 9),
            (15, 10, 14.5),
        ]
    )
    swings = find_swings(bars, strength=2)

    after_everything = detect_structure_shift(
        bars, swings, direction=Direction.LONG, after_ts=bars[-1].ts
    )

    assert after_everything is None


def test_direction_opposite_is_symmetric():
    assert Direction.LONG.opposite is Direction.SHORT
    assert Direction.SHORT.opposite is Direction.LONG
