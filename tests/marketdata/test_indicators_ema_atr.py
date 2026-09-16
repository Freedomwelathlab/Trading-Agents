"""EMA and ATR (Phase 70, D088), against values computed BY HAND.

Every expected number below is derived in the comment beside it from the
definition in the docstring of the function under test - never captured
from a run of that function. A test whose expectation came from the code it
tests proves only that the code is deterministic, which was never in doubt;
it would have happily locked in an off-by-one in the seeding or a true
range that ignored the previous close. That discipline is the same one
tests/marketdata/test_indicators.py already applies to SMA and RSI.
"""

from decimal import Decimal

import pytest

from apps.api.app.marketdata.indicators import InsufficientDataError, atr, ema


def _d(*values: object) -> list[Decimal]:
    return [Decimal(str(v)) for v in values]


class _Bar:
    """The three fields ATR reads. Stands in for `marketdata.bar_provider.Bar`
    on purpose: `atr` takes a structural Protocol, so this test needs no
    Pydantic model, no symbol, no timestamp and no vendor - which is what
    lets each case below be read as a plain table of numbers."""

    def __init__(
        self,
        high: object = None,
        low: object = None,
        close: object = None,
    ) -> None:
        self.high = None if high is None else Decimal(str(high))
        self.low = None if low is None else Decimal(str(low))
        self.close = None if close is None else Decimal(str(close))


# ------------------------------------------------------------------- EMA


def test_ema_of_exactly_period_closes_is_their_simple_average() -> None:
    # With no closes after the seed, an EMA has done no smoothing at all:
    # it is the SMA it was seeded from. (2 + 4 + 6) / 3 = 4.
    assert ema(_d(2, 4, 6), 3) == Decimal(4)


def test_ema_steps_forward_by_the_smoothing_factor() -> None:
    # period 3 -> multiplier 2 / (3 + 1) = 0.5.
    #   seed  = SMA(1, 2, 3)          = 2
    #   at 4  = (4 - 2) * 0.5 + 2     = 3
    #   at 5  = (5 - 3) * 0.5 + 3     = 4
    assert ema(_d(1, 2, 3, 4, 5), 3) == Decimal(4)


def test_ema_weights_recent_closes_more_heavily_than_sma_does() -> None:
    # The property that makes EMA worth having. Same five closes, with a
    # jump at the end: EMA(3) must sit ABOVE SMA(3) of the same window
    # because it has been tracking the rise rather than averaging it flat.
    #   multiplier 0.5, seed = SMA(10, 10, 10) = 10
    #   at 20 = (20 - 10) * 0.5 + 10 = 15
    #   at 30 = (30 - 15) * 0.5 + 15 = 22.5
    closes = _d(10, 10, 10, 20, 30)
    assert ema(closes, 3) == Decimal("22.5")
    # SMA(3) over the last three is (10 + 20 + 30) / 3 = 20.
    assert ema(closes, 3) > Decimal(20)


def test_ema_of_a_flat_series_is_that_flat_value() -> None:
    # No drift from repeated smoothing: a constant in is that constant out,
    # at any length. This is the case a wrong multiplier tends to survive,
    # so it is asserted over a long series rather than a short one.
    assert ema(_d(*([Decimal(7)] * 50)), 10) == Decimal(7)


def test_ema_needs_period_closes_not_period_plus_one() -> None:
    # Unlike RSI, EMA measures levels rather than changes, so there is no
    # extra bar to pay for. Exactly `period` is enough; one fewer is not.
    assert ema(_d(1, 2, 3), 3) == Decimal(2)
    with pytest.raises(InsufficientDataError, match=r"EMA\(3\) needs 3 closes, got 2"):
        ema(_d(1, 2), 3)


def test_ema_rejects_a_period_below_one() -> None:
    with pytest.raises(ValueError, match="period must be at least 1"):
        ema(_d(1, 2, 3), 0)


# ------------------------------------------------------------------- ATR


def test_atr_true_range_is_the_widest_of_the_three_measures() -> None:
    # One true range, computed by hand from
    #   max(high - low, |high - prev_close|, |low - prev_close|)
    #
    #   prev close 100
    #   bar: high 110, low 105  ->  high-low        = 5
    #                               |110 - 100|     = 10   <- widest
    #                               |105 - 100|     = 5
    # A gapped-up bar whose own body is narrow: the plain high-minus-low
    # (5) understates the day by half, which is exactly the case the two
    # prev_close terms exist to catch.
    bars = [_Bar(101, 99, 100), _Bar(110, 105, 108)]
    assert atr(bars, 1) == Decimal(10)


def test_atr_is_the_simple_average_of_the_true_ranges() -> None:
    #   prev close 10
    #   bar A: high 12, low 10, close 11 -> max(2, |12-10|=2, |10-10|=0) = 2
    #   bar B: high 15, low 11, close 14 -> max(4, |15-11|=4, |11-11|=0) = 4
    #   bar C: high 15, low 13, close 13 -> max(2, |15-14|=1, |13-14|=1) = 2
    #   (2 + 4 + 2) / 3 = 8 / 3
    bars = [_Bar(11, 9, 10), _Bar(12, 10, 11), _Bar(15, 11, 14), _Bar(15, 13, 13)]
    assert atr(bars, 3) == Decimal(8) / Decimal(3)


def test_atr_uses_only_the_most_recent_period_true_ranges() -> None:
    # Older, wilder bars must not leak into the answer. The first three bars
    # here are violent; the last two are calm, and ATR(1) sees only the last
    # true range: max(1, |101-100|, |100-100|) = 1.
    bars = [_Bar(200, 10, 150), _Bar(300, 20, 40), _Bar(250, 30, 100), _Bar(101, 100, 100)]
    assert atr(bars, 1) == Decimal(1)


def test_atr_needs_period_plus_one_bars() -> None:
    # The first true range needs a bar before it to take a previous close
    # from, exactly as RSI needs period + 1 closes to measure period changes.
    with pytest.raises(InsufficientDataError, match=r"ATR\(3\) needs 4 bars, got 3"):
        atr([_Bar(11, 9, 10), _Bar(12, 10, 11), _Bar(13, 11, 12)], 3)


def test_atr_refuses_a_bar_with_no_high_or_low_rather_than_estimating_one() -> None:
    # `market_data_bars.high`/`.low` are NULLABLE - a vendor may return a
    # close-only record - so this is a real case, not a defensive one. The
    # only alternative to refusing is to invent a range from the close,
    # which would be fabricating market data (docs/TRADING_SAFETY.md).
    bars = [_Bar(11, 9, 10), _Bar(high=None, low=None, close=11)]
    with pytest.raises(InsufficientDataError, match="close-only bar"):
        atr(bars, 1)


def test_atr_refuses_when_the_PREVIOUS_bar_has_no_close() -> None:
    # A distinct gap from the one above: this bar is complete, but the bar
    # it must measure a gap against is not. Caught separately so the message
    # names the real problem.
    bars = [_Bar(11, 9, None), _Bar(12, 10, 11)]
    with pytest.raises(InsufficientDataError, match="previous bar's close"):
        atr(bars, 1)


def test_atr_of_identical_bars_is_zero() -> None:
    # Zero volatility is a real answer here, not a missing one - and it is
    # the value a stop-loss sized off ATR would have to handle. Asserted so
    # that "no movement" can never be confused with "no data", which raises.
    bars = [_Bar(10, 10, 10) for _ in range(5)]
    assert atr(bars, 3) == Decimal(0)


def test_atr_rejects_a_period_below_one() -> None:
    with pytest.raises(ValueError, match="period must be at least 1"):
        atr([_Bar(11, 9, 10), _Bar(12, 10, 11)], 0)
