"""Candlestick pattern tests (Phase 76, D094).

Each test pins the ASTA material's exact rule, including the boundaries
that separate one pattern from a stronger neighbour (piercing vs engulf)
and the trend-context rule that a reversal shape is only a signal at the
end of a trend.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apps.api.app.marketdata.candles import (
    PatternDirection,
    Trend,
    detect_at,
    is_bullish_engulf,
    is_bullish_piercing,
    is_doji,
    is_hammer,
    is_morning_star,
    is_shooting_star,
    is_three_white_soldiers,
)

T0 = datetime(2026, 6, 17, 13, 30, tzinfo=UTC)


class Bar:
    def __init__(self, i, o, h, low, c):
        self.ts = T0 + timedelta(minutes=5 * i)
        self.open = Decimal(str(o))
        self.high = Decimal(str(h))
        self.low = Decimal(str(low))
        self.close = Decimal(str(c))
        self.volume = 1000


def test_hammer_needs_lower_wick_at_least_twice_the_body():
    # body 1 (10->11), lower wick 3 (7->10), tiny upper wick -> hammer.
    assert is_hammer(Bar(0, 10, 11.2, 7, 11))
    # Same body but a short lower wick -> not a hammer.
    assert not is_hammer(Bar(0, 10, 11.2, 9.5, 11))


def test_shooting_star_is_the_mirror_of_the_hammer():
    assert is_shooting_star(Bar(0, 11, 14, 10.8, 10.5))  # long upper wick, small body low in range
    assert not is_shooting_star(Bar(0, 10, 11.2, 7, 11))  # that's a hammer


def test_doji_is_a_near_equal_open_and_close():
    assert is_doji(Bar(0, 100, 103, 97, 100.1))     # body 0.1 vs range 6
    assert not is_doji(Bar(0, 100, 103, 97, 102.5))  # big body


def test_bullish_engulf_closes_above_the_prior_open():
    prev = Bar(0, 105, 106, 100, 101)   # bearish
    cur = Bar(1, 100.5, 108, 100, 106)  # bullish, closes above prev open (105)
    assert is_bullish_engulf(prev, cur)


def test_piercing_is_above_the_median_but_not_a_full_engulf():
    prev = Bar(0, 110, 111, 100, 100)   # bearish, body 110->100, median 105
    # Closes at 106: above median (105), below open (110) -> piercing, not engulf.
    cur = Bar(1, 99, 107, 99, 106)
    assert is_bullish_piercing(prev, cur)
    assert not is_bullish_engulf(prev, cur)
    # Closes at 111: above the open -> engulf, and NOT piercing (mutually exclusive).
    cur2 = Bar(1, 99, 112, 99, 111)
    assert is_bullish_engulf(prev, cur2)
    assert not is_bullish_piercing(prev, cur2)


def test_a_close_below_the_median_is_neither_piercing_nor_engulf():
    prev = Bar(0, 110, 111, 100, 100)   # median 105
    weak = Bar(1, 99, 104, 99, 103)     # closes at 103, below median
    assert not is_bullish_piercing(prev, weak)
    assert not is_bullish_engulf(prev, weak)


def test_morning_star_needs_a_small_middle_candle_and_a_strong_third():
    a = Bar(0, 110, 111, 100, 100)      # bearish, median 105
    b = Bar(1, 99, 100, 97, 98.5)       # small star, gaps down
    c = Bar(2, 99, 107, 98, 106)        # bullish, closes above a's median
    assert is_morning_star(a, b, c)
    # A large middle candle disqualifies it.
    big_mid = Bar(1, 99, 108, 96, 107)
    assert not is_morning_star(a, big_mid, c)


def test_three_white_soldiers_each_close_higher():
    a, b, c = Bar(0, 100, 103, 99, 102), Bar(1, 101, 105, 100, 104), Bar(2, 103, 107, 102, 106)
    assert is_three_white_soldiers(a, b, c)
    stalled = Bar(2, 103, 107, 102, 103.5)  # closes below b
    assert not is_three_white_soldiers(a, b, stalled)


def test_reversal_shape_is_only_a_signal_at_the_end_of_its_trend():
    # A hammer detected mid-uptrend is NOT a buy signal per the material.
    bars = [Bar(0, 10, 11.2, 7, 11)]
    (hammer,) = [p for p in detect_at(bars, 0) if p.name == "hammer"]
    assert hammer.direction is PatternDirection.BULLISH
    assert hammer.requires_trend is Trend.DOWN
    assert hammer.is_signal_in_context(Trend.DOWN) is True
    assert hammer.is_signal_in_context(Trend.UP) is False
    assert hammer.is_signal_in_context(Trend.SIDEWAYS) is False


def test_detect_at_reports_patterns_on_the_completing_bar_only():
    # Three rising bullish candles: the soldiers pattern completes on index 2.
    bars = [
        Bar(0, 100, 103, 99, 102),
        Bar(1, 101, 105, 100, 104),
        Bar(2, 103, 107, 102, 106),
    ]
    assert not any(p.name == "three_white_soldiers" for p in detect_at(bars, 1))
    assert any(p.name == "three_white_soldiers" for p in detect_at(bars, 2))


def test_a_doji_is_neutral_and_needs_no_trend_context():
    (doji,) = [p for p in detect_at([Bar(0, 100, 103, 97, 100.1)], 0) if p.name == "doji"]
    assert doji.direction is PatternDirection.NEUTRAL
    assert doji.requires_trend is None
    assert doji.is_signal_in_context(Trend.SIDEWAYS) is True


def test_close_only_bar_is_refused_rather_than_guessed():
    import pytest

    class CloseOnly:
        ts = T0
        open = None
        high = None
        low = None
        close = Decimal("100")
        volume = None

    with pytest.raises(ValueError, match="open price"):
        detect_at([CloseOnly()], 0)
