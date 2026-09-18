"""Candlestick setup wiring tests (Phase 77, D095).

The ASTA material gates a candlestick reversal behind TWO things, and a
shape alone is not a trade. These tests pin both gates and the Dow trend
they depend on, because a setup that fires on shape alone would trade every
hammer in a range.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apps.api.app.backtesting.setups import (
    BarContext,
    candle_reversal_setup,
    dow_trend,
)
from apps.api.app.marketdata.candles import Trend
from apps.api.app.marketdata.sessions import SessionLevels
from apps.api.app.marketdata.structure import Direction, SwingKind, SwingPoint

T0 = datetime(2026, 6, 17, 13, 30, tzinfo=UTC)


class Bar:
    def __init__(self, i, o, h, low, c):
        self.ts = T0 + timedelta(minutes=5 * i)
        self.open = Decimal(str(o))
        self.high = Decimal(str(h))
        self.low = Decimal(str(low))
        self.close = Decimal(str(c))
        self.volume = 1000


def swing(kind, i, price):
    """Confirmed two bars after it printed, matching find_swings."""
    return SwingPoint(
        kind=kind,
        ts=T0 + timedelta(minutes=5 * i),
        price=Decimal(str(price)),
        confirmed_ts=T0 + timedelta(minutes=5 * (i + 2)),
    )


def downtrend_swings():
    """Lower highs AND lower lows -> Dow downtrend."""
    return [
        swing(SwingKind.HIGH, 0, 120), swing(SwingKind.LOW, 1, 110),
        swing(SwingKind.HIGH, 2, 115), swing(SwingKind.LOW, 3, 105),
    ]


def uptrend_swings():
    return [
        swing(SwingKind.HIGH, 0, 100), swing(SwingKind.LOW, 1, 95),
        swing(SwingKind.HIGH, 2, 110), swing(SwingKind.LOW, 3, 105),
    ]


def make_ctx(bars, *, swings, levels=None, atr="1", rsi=None, index=None):
    return BarContext(
        bars=bars,
        index=len(bars) - 1 if index is None else index,
        levels=levels
        or SessionLevels(session_date=T0.date(), previous_low=Decimal("100")),
        swings=swings,
        vwap=None,
        atr=Decimal(atr),
        rsi=Decimal(rsi) if rsi else None,
        ema_fast=None,
        ema_slow=None,
    )


def hammer_bars():
    """A genuine hammer as the LAST bar, on a series long enough that the
    swings above are already confirmed at that bar.

    The hammer: open 102, close 102.5 (body 0.5), high 102.7 (upper wick
    0.2 <= body), low 100.0 (lower wick 2.0 >= 2x body). Its low sits
    exactly on the previous-day low of 100, so the location gate is met.
    A zero-body candle is deliberately NOT a hammer, which an earlier
    version of this fixture got wrong.
    """
    filler = [Bar(i, 106 - i * 0.4, 106.5 - i * 0.4, 105 - i * 0.4, 105.5 - i * 0.4)
              for i in range(8)]
    return [*filler, Bar(8, 102, 102.7, 100, 102.5)]


def test_dow_trend_is_higher_highs_and_higher_lows():
    ctx_up = make_ctx(hammer_bars(), swings=uptrend_swings())
    ctx_down = make_ctx(hammer_bars(), swings=downtrend_swings())
    assert dow_trend(ctx_up) is Trend.UP
    assert dow_trend(ctx_down) is Trend.DOWN


def test_mixed_swings_are_sideways_not_a_guessed_trend():
    mixed = [
        swing(SwingKind.HIGH, 0, 100), swing(SwingKind.LOW, 1, 95),
        swing(SwingKind.HIGH, 2, 110), swing(SwingKind.LOW, 3, 90),  # HH but LL
    ]
    assert dow_trend(make_ctx(hammer_bars(), swings=mixed)) is Trend.SIDEWAYS


def test_too_few_confirmed_swings_is_sideways():
    """With no established trend the honest answer is SIDEWAYS, which makes
    every reversal pattern fail its context gate rather than fire blind."""
    thin = [swing(SwingKind.HIGH, 0, 100), swing(SwingKind.LOW, 1, 95)]
    assert dow_trend(make_ctx(hammer_bars(), swings=thin)) is Trend.SIDEWAYS


def test_unconfirmed_swings_do_not_set_the_trend():
    """A swing not yet confirmed at this bar must not count — the same
    look-ahead guard structure.py applies."""
    future = [
        SwingPoint(SwingKind.HIGH, T0, Decimal("120"), T0 + timedelta(hours=5)),
        SwingPoint(SwingKind.LOW, T0, Decimal("110"), T0 + timedelta(hours=5)),
        SwingPoint(SwingKind.HIGH, T0, Decimal("115"), T0 + timedelta(hours=5)),
        SwingPoint(SwingKind.LOW, T0, Decimal("105"), T0 + timedelta(hours=5)),
    ]
    assert dow_trend(make_ctx(hammer_bars(), swings=future)) is Trend.SIDEWAYS


def test_hammer_at_a_level_in_a_downtrend_is_a_long_setup():
    ctx = make_ctx(hammer_bars(), swings=downtrend_swings())
    signal = candle_reversal_setup(ctx)

    assert signal is not None
    assert signal.setup_name == "candle_reversal"
    assert signal.direction is Direction.LONG
    assert signal.evidence["pattern"] == "hammer"
    assert "PDL" in signal.evidence["location"]
    # Stop sits beyond the pattern's own low (99) plus the ATR buffer.
    assert signal.stop_price < Decimal("100")


def test_the_same_hammer_in_an_uptrend_is_not_a_signal():
    """The material's rule: a reversal candle is significant only at the end
    of a trend. Mid-uptrend, the identical shape must not trade."""
    ctx = make_ctx(hammer_bars(), swings=uptrend_swings())
    assert candle_reversal_setup(ctx) is None


def test_the_same_hammer_in_open_space_is_not_a_signal():
    """PAPA orders it location first. With no marked level nearby, the
    pattern is not a setup."""
    no_levels = SessionLevels(session_date=T0.date())  # every level None
    ctx = make_ctx(hammer_bars(), swings=downtrend_swings(), levels=no_levels)
    assert candle_reversal_setup(ctx) is None
    # ...and with the location gate relaxed it does fire, proving the gate
    # is what suppressed it rather than the pattern being undetected.
    assert candle_reversal_setup(ctx, require_location=False) is not None


def test_a_null_level_is_never_treated_as_a_price_of_zero():
    """A previous low of None must not be read as 0.00 — that would sit
    below every bar and make 'at the level' spuriously true."""
    only_nulls = SessionLevels(session_date=T0.date(), previous_low=None)
    ctx = make_ctx(hammer_bars(), swings=downtrend_swings(), levels=only_nulls)
    assert candle_reversal_setup(ctx) is None


def test_rsi_exhaustion_raises_the_score():
    plain = candle_reversal_setup(make_ctx(hammer_bars(), swings=downtrend_swings()))
    oversold = candle_reversal_setup(
        make_ctx(hammer_bars(), swings=downtrend_swings(), rsi="25")
    )
    assert oversold.score > plain.score
    assert "rsi" in oversold.evidence


def test_a_close_only_bar_is_skipped_rather_than_crashing_the_replay():
    class CloseOnly:
        ts = T0 + timedelta(minutes=10)
        open = None
        high = None
        low = None
        close = Decimal("100")
        volume = None

    bars = [*hammer_bars()[:2], CloseOnly()]
    ctx = make_ctx(bars, swings=downtrend_swings())
    assert candle_reversal_setup(ctx) is None
