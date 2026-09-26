"""Measured signal confidence (Phase 98, D117)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apps.api.app.backtesting.setups import SetupSignal
from apps.api.app.backtesting.signal_calibration import (
    MIN_SAMPLE,
    ScannedSignal,
    _outcome,
    _phase_bars,
    bucket_key,
    calibrate,
)
from apps.api.app.marketdata.structure import Direction

T0 = datetime(2026, 9, 24, 14, 0, tzinfo=UTC)


@dataclass
class Bar:
    ts: datetime
    high: Decimal
    low: Decimal


def bars(*hl):
    return [
        Bar(T0 + timedelta(minutes=5 * i), Decimal(h), Decimal(lo)) for i, (h, lo) in enumerate(hl)
    ]


def signal(direction=Direction.LONG, entry="100", stop="99", score=3, name="x"):
    return SetupSignal(
        setup_name=name,
        direction=direction,
        entry_index=0,
        entry_price=Decimal(entry),
        stop_price=Decimal(stop),
        score=score,
        evidence={},
    )


def test_a_long_wins_only_when_one_r_comes_before_the_stop():
    s = signal()  # risk 1 -> target 101
    assert _outcome(bars(("100", "99.5"), ("101", "99.6")), 0, s) == "win"
    assert _outcome(bars(("100", "99.5"), ("100.5", "98.9")), 0, s) == "loss"
    assert _outcome(bars(("100", "99.5"), ("100.5", "99.5")), 0, s) == "open"


def test_a_bar_spanning_both_counts_as_a_loss():
    # A 5-minute bar cannot say which came first; assume the worse.
    assert _outcome(bars(("100", "99.5"), ("101.5", "98.5")), 0, signal()) == "loss"


def test_a_short_is_mirrored():
    s = signal(Direction.SHORT, entry="100", stop="101")  # target 99
    assert _outcome(bars(("100", "99.5"), ("100.2", "98.9")), 0, s) == "win"


def test_confidence_needs_a_minimum_sample_and_ignores_open_signals():
    s = signal()
    wins = [ScannedSignal(s, T0, "regular", "win")] * (MIN_SAMPLE - 1)
    opens = [ScannedSignal(s, T0, "regular", "open")] * 5
    thin = calibrate(wins + opens)[bucket_key(s)]
    assert thin.resolved == MIN_SAMPLE - 1 and thin.win_rate is None

    enough = calibrate(wins + [ScannedSignal(s, T0, "regular", "loss")])[bucket_key(s)]
    assert enough.resolved == MIN_SAMPLE
    assert enough.win_rate == Decimal("90.0")


def test_buckets_are_per_setup_direction_and_score():
    a = signal(score=3)
    b = signal(score=4)
    got = calibrate(
        [ScannedSignal(a, T0, "regular", "win"), ScannedSignal(b, T0, "regular", "loss")]
    )
    assert set(got) == {("x", "long", 3), ("x", "long", 4)}


def test_auto_scans_the_extended_day_and_regular_does_not():
    day = [
        Bar(datetime(2026, 9, 24, 12, 0, tzinfo=UTC), Decimal(1), Decimal(1)),  # pre-market
        Bar(datetime(2026, 9, 24, 15, 0, tzinfo=UTC), Decimal(1), Decimal(1)),  # regular
        Bar(datetime(2026, 9, 24, 21, 0, tzinfo=UTC), Decimal(1), Decimal(1)),  # after hours
    ]
    assert len(_phase_bars(day, "auto")) == 3
    assert len(_phase_bars(day, "regular")) == 1
