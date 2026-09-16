"""Session-structure tests (Phase 73, D091).

These pin the RULES rather than the numbers: which bar counts as the
open, what "previous day" resolves to across a weekend or a holiday, and
what happens when a level has no data behind it. Every one of them
describes a failure that would otherwise be silent - a level computed
from the wrong bars still charts, still validates, and still produces a
confident backtest about the wrong thing.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from apps.api.app.marketdata.sessions import (
    SessionPhase,
    build_session_levels,
    group_by_session,
    is_regular_hours,
    session_date,
    session_phase,
    session_vwap,
)


class Bar:
    def __init__(self, ts, *, open=None, high=None, low=None, close, volume=1_000):
        self.ts = ts
        self.open = Decimal(str(open)) if open is not None else None
        self.high = Decimal(str(high)) if high is not None else None
        self.low = Decimal(str(low)) if low is not None else None
        self.close = Decimal(str(close))
        self.volume = volume


def et_bar(day: date, hh: int, mm: int, **kw) -> Bar:
    """A bar at a wall-clock EXCHANGE time, converted to the UTC instant a
    real feed would carry. Written this way on purpose: hand-writing the
    UTC hour would bake one DST offset into the fixture and hide exactly
    the bug these tests exist to catch."""
    from zoneinfo import ZoneInfo

    local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=ZoneInfo("America/New_York"))
    return Bar(local.astimezone(UTC), **kw)


# --------------------------------------------------------------------------
# Phase classification


def test_the_regular_session_is_the_same_wall_clock_on_both_sides_of_dst():
    """The whole reason classification runs in America/New_York.

    09:30 ET is 13:30 UTC in July and 14:30 UTC in January. A rule written
    against a fixed UTC hour is right for one of these and an hour out for
    the other - and an hour out means the "first fifteen minutes" lands on
    the wrong bars for about a third of any year-long backtest, with
    nothing anywhere reporting a problem.
    """
    summer_open = datetime(2026, 7, 15, 13, 30, tzinfo=UTC)
    winter_open = datetime(2026, 1, 15, 14, 30, tzinfo=UTC)

    assert session_phase(summer_open) is SessionPhase.REGULAR
    assert session_phase(winter_open) is SessionPhase.REGULAR

    # And one minute earlier is still premarket in both regimes.
    assert session_phase(summer_open - timedelta(minutes=5)) is SessionPhase.PRE_MARKET
    assert session_phase(winter_open - timedelta(minutes=5)) is SessionPhase.PRE_MARKET


@pytest.mark.parametrize(
    ("hh", "mm", "expected"),
    [
        (3, 55, None),                      # before the extended day opens
        (4, 0, SessionPhase.PRE_MARKET),    # premarket opens
        (9, 25, SessionPhase.PRE_MARKET),
        (9, 30, SessionPhase.REGULAR),      # the open belongs to the session
        (15, 55, SessionPhase.REGULAR),     # last regular 5m bar
        (16, 0, SessionPhase.AFTER_HOURS),  # the close bar is after-hours
        (19, 55, SessionPhase.AFTER_HOURS),
        (20, 0, None),                      # extended day is over
    ],
)
def test_phase_boundaries_are_half_open_on_the_opening_edge(hh, mm, expected):
    """A bar is classified by the instant it OPENS, so 09:30 is the first
    regular bar and 16:00 is not one. That convention makes a 5-minute
    regular session exactly 78 bars, which is the count this was verified
    against on real TQQQ data."""
    assert session_phase(et_bar(date(2026, 6, 17), hh, mm, close=1).ts) is expected


def test_session_date_is_the_exchange_date_not_the_utc_date():
    """A summer after-hours bar at 19:55 ET is 23:55 UTC the same day, but
    the 20:00 ET print is 00:00 UTC the NEXT day. Keying on the UTC date
    files the tail of one session under the following one, which shows up
    later as a session with a strange opening range and a neighbour
    missing its close."""
    late = et_bar(date(2026, 6, 17), 19, 55, close=1)
    assert late.ts.date() == date(2026, 6, 17)  # still the same UTC date here
    assert session_date(late.ts) == date(2026, 6, 17)

    # Winter pushes the same wall-clock bar over the UTC midnight line.
    winter_late = et_bar(date(2026, 1, 15), 19, 55, close=1)
    assert winter_late.ts.date() == date(2026, 1, 16)  # UTC date has rolled
    assert session_date(winter_late.ts) == date(2026, 1, 15)  # session has not


def test_a_naive_timestamp_is_refused_rather_than_assumed_to_be_utc():
    with pytest.raises(ValueError, match="aware timestamp"):
        session_phase(datetime(2026, 6, 17, 13, 30))


# --------------------------------------------------------------------------
# Levels


def _session(day: date, *, pre=(), regular=(), post=()) -> list[Bar]:
    bars = []
    for hh, mm, price in pre:
        bars.append(et_bar(day, hh, mm, open=price, high=price, low=price, close=price))
    for hh, mm, price in regular:
        bars.append(et_bar(day, hh, mm, open=price, high=price, low=price, close=price))
    for hh, mm, price in post:
        bars.append(et_bar(day, hh, mm, open=price, high=price, low=price, close=price))
    return bars


def test_previous_day_levels_come_from_regular_hours_only():
    """PDH/PDL are the levels a trader marks, and those are the regular
    session's extremes. Folding in after-hours would move them to a print
    that traded a handful of shares at a spread nobody could hit - and the
    strategy would then wait for a sweep of a level that never really
    existed."""
    day_one = _session(
        date(2026, 6, 16),
        pre=[(8, 0, 50)],
        regular=[(9, 30, 100), (12, 0, 110), (15, 55, 105)],
        post=[(18, 0, 999)],  # an after-hours spike that must NOT become PDH
    )
    day_two = _session(date(2026, 6, 17), regular=[(9, 30, 106)])

    levels = build_session_levels([*day_one, *day_two])

    assert levels[date(2026, 6, 17)].previous_high == Decimal("110")
    assert levels[date(2026, 6, 17)].previous_low == Decimal("100")
    assert levels[date(2026, 6, 17)].previous_close == Decimal("105")


def test_previous_day_skips_the_weekend_and_a_holiday():
    """Calendar arithmetic gives a Monday a previous day of Sunday and a
    post-holiday session a previous day that never traded. Both yield empty
    levels, and an empty PDH reads downstream as "price never swept the
    level" - a silent no-trade rather than a visible gap.

    Verified against real data too: TQQQ's Monday 2026-04-06 resolves to
    Thursday 2026-04-02, correctly stepping over Good Friday, with no
    holiday calendar anywhere in this module.
    """
    thursday = _session(date(2026, 4, 2), regular=[(9, 30, 40), (15, 55, 43)])
    # Friday 2026-04-03 is Good Friday - no session at all.
    monday = _session(date(2026, 4, 6), regular=[(9, 30, 44)])

    levels = build_session_levels([*thursday, *monday])

    assert levels[date(2026, 4, 6)].previous_high == Decimal("43")
    assert levels[date(2026, 4, 6)].previous_close == Decimal("43")


def test_the_first_session_has_no_previous_day_and_says_so():
    """`None`, not zero and not the session's own open. A zero PDL is below
    every price, so "price swept the previous low" would be permanently
    false; a PDL defaulted to the open would make it spuriously true."""
    levels = build_session_levels(_session(date(2026, 6, 17), regular=[(9, 30, 100)]))
    day = levels[date(2026, 6, 17)]

    assert day.previous_high is None
    assert day.previous_low is None
    assert day.has_previous_day is False


def test_the_opening_range_covers_only_bars_inside_the_window():
    """15 minutes from 09:30 is three 5-minute bars: 09:30, 09:35, 09:40.
    The 09:45 bar opens the window's end and is excluded, the same
    half-open convention the phase boundaries use."""
    bars = _session(
        date(2026, 6, 17),
        regular=[(9, 30, 100), (9, 35, 104), (9, 40, 99), (9, 45, 130), (10, 0, 90)],
    )

    levels = build_session_levels(bars)[date(2026, 6, 17)]

    assert levels.opening_range_high == Decimal("104")
    assert levels.opening_range_low == Decimal("99")


def test_the_opening_range_window_is_configurable():
    """The playbook names 15 minutes as a starting value and 30 as the
    alternative to test, explicitly declining to call either optimal."""
    bars = _session(
        date(2026, 6, 17),
        regular=[(9, 30, 100), (9, 40, 104), (9, 50, 130), (10, 30, 90)],
    )

    fifteen = build_session_levels(bars, opening_range_minutes=15)[date(2026, 6, 17)]
    thirty = build_session_levels(bars, opening_range_minutes=30)[date(2026, 6, 17)]

    assert fifteen.opening_range_high == Decimal("104")
    assert thirty.opening_range_high == Decimal("130")


def test_premarket_levels_are_separate_from_the_opening_range():
    bars = _session(
        date(2026, 6, 17),
        pre=[(4, 0, 95), (8, 0, 97), (9, 25, 96)],
        regular=[(9, 30, 100), (9, 35, 101)],
    )

    levels = build_session_levels(bars)[date(2026, 6, 17)]

    assert levels.premarket_high == Decimal("97")
    assert levels.premarket_low == Decimal("95")
    assert levels.opening_range_high == Decimal("101")


def test_a_session_that_never_traded_premarket_reports_no_premarket_level():
    levels = build_session_levels(
        _session(date(2026, 6, 17), regular=[(9, 30, 100)])
    )[date(2026, 6, 17)]

    assert levels.premarket_high is None
    assert levels.premarket_low is None


def test_a_gap_day_carries_the_last_real_session_forward_as_previous_day():
    """A session with no regular bars must not become the next day's
    "previous day". The level traders are watching does not vanish because
    a feed dropped a morning."""
    real = _session(date(2026, 6, 15), regular=[(9, 30, 100), (15, 55, 108)])
    premarket_only = _session(date(2026, 6, 16), pre=[(8, 0, 105)])
    later = _session(date(2026, 6, 17), regular=[(9, 30, 107)])

    levels = build_session_levels([*real, *premarket_only, *later])

    assert levels[date(2026, 6, 17)].previous_high == Decimal("108")
    assert levels[date(2026, 6, 16)].previous_high == Decimal("108")


def test_grouping_orders_each_session_regardless_of_input_order():
    shuffled = [
        et_bar(date(2026, 6, 17), 15, 55, close=3),
        et_bar(date(2026, 6, 17), 9, 30, close=1),
        et_bar(date(2026, 6, 17), 12, 0, close=2),
    ]
    grouped = group_by_session(shuffled)
    assert [b.close for b in grouped[date(2026, 6, 17)]] == [
        Decimal("1"),
        Decimal("2"),
        Decimal("3"),
    ]


def test_a_non_positive_opening_range_is_refused():
    with pytest.raises(ValueError, match="must be positive"):
        build_session_levels([], opening_range_minutes=0)


# --------------------------------------------------------------------------
# VWAP


def test_vwap_is_volume_weighted_not_a_price_average():
    """The distinction that makes VWAP a location rather than a moving
    average: one large trade moves it more than several small ones."""
    bars = [
        et_bar(date(2026, 6, 17), 9, 30, high=10, low=10, close=10, volume=1),
        et_bar(date(2026, 6, 17), 9, 35, high=20, low=20, close=20, volume=99),
    ]
    points = session_vwap(bars)

    assert points[-1].vwap == Decimal("1990") / Decimal(100)  # 19.9, not 15


def test_vwap_resets_at_each_session_and_ignores_premarket_by_default():
    """"Session VWAP" on a US equity chart starts at 09:30. Anchoring it
    earlier drags it toward a few thousand thin premarket shares and shifts
    every deviation band with it."""
    bars = [
        et_bar(date(2026, 6, 16), 8, 0, high=1, low=1, close=1, volume=10_000),
        et_bar(date(2026, 6, 16), 9, 30, high=10, low=10, close=10, volume=100),
        et_bar(date(2026, 6, 17), 9, 30, high=50, low=50, close=50, volume=100),
    ]
    points = session_vwap(bars)

    assert len(points) == 2  # the premarket bar contributed nothing
    assert points[0].vwap == Decimal("10")
    assert points[1].vwap == Decimal("50")  # new session, not a running mean


def test_a_zero_volume_bar_does_not_move_vwap():
    """A volume-weighted mean gives an untraded print zero weight. Counting
    it as a data point would let a bar nobody traded move a level that
    exists to describe where volume actually went."""
    bars = [
        et_bar(date(2026, 6, 17), 9, 30, high=10, low=10, close=10, volume=100),
        et_bar(date(2026, 6, 17), 9, 35, high=999, low=999, close=999, volume=0),
    ]
    points = session_vwap(bars)

    assert len(points) == 1
    assert points[0].vwap == Decimal("10")


def test_sigma_is_zero_on_a_single_price_and_bands_collapse_onto_vwap():
    """The floor exists because the two cumulants are tracked
    independently, so rounding can leave `E[p²] - E[p]²` fractionally
    negative on a session that printed at one price - and `Decimal.sqrt` of
    a negative raises rather than returning a complex number."""
    bars = [
        et_bar(date(2026, 6, 17), 9, 30, high=10, low=10, close=10, volume=100),
        et_bar(date(2026, 6, 17), 9, 35, high=10, low=10, close=10, volume=250),
    ]
    points = session_vwap(bars)

    assert points[-1].sigma == Decimal(0)
    assert points[-1].band(2) == (Decimal("10"), Decimal("10"))


def test_bands_are_returned_as_a_matched_pair():
    bars = [
        et_bar(date(2026, 6, 17), 9, 30, high=10, low=10, close=10, volume=100),
        et_bar(date(2026, 6, 17), 9, 35, high=20, low=20, close=20, volume=100),
    ]
    point = session_vwap(bars)[-1]
    lower, upper = point.band(2)

    assert lower == point.vwap - point.sigma * 2
    assert upper == point.vwap + point.sigma * 2
    assert lower < point.vwap < upper


def test_regular_hours_gate_matches_phase_classification():
    assert is_regular_hours(et_bar(date(2026, 6, 17), 9, 30, close=1).ts)
    assert not is_regular_hours(et_bar(date(2026, 6, 17), 9, 25, close=1).ts)
    assert not is_regular_hours(et_bar(date(2026, 6, 17), 16, 0, close=1).ts)
