"""FX symbols, the 24h FX session clock and resampling (Phase 103, D123)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.fx import (
    NotAnFxSymbolError,
    calendar_for_symbol,
    fx_pair,
    ig_epic_for,
    is_fx_symbol,
    pair_spec,
    resample_bars,
)
from apps.api.app.marketdata.providers.histdata import (
    HistDataFormatError,
    parse_line,
)
from apps.api.app.marketdata.sessions import (
    FX_CALENDAR,
    US_EQUITY_CALENDAR,
    FxCalendar,
    SessionPhase,
    build_session_levels,
    group_by_session,
    session_vwap,
)

NY = ZoneInfo("America/New_York")
LDN = ZoneInfo("Europe/London")


def ny(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


# --- symbols -----------------------------------------------------------------


def test_fx_symbol_convention():
    assert is_fx_symbol("EURUSD.FX")
    assert is_fx_symbol("usdjpy.fx")
    for other in ("TQQQ.US", "BTC-USD", "EURUSD", "EUR.FX", "EURUSD.US"):
        assert not is_fx_symbol(other)
    assert fx_pair("eurusd.fx") == "EURUSD"
    with pytest.raises(NotAnFxSymbolError):
        fx_pair("TQQQ.US")


def test_ig_epic_mapping_uses_the_template():
    assert ig_epic_for("EURUSD.FX") == "CS.D.EURUSD.MINI.IP"
    assert ig_epic_for("GBPUSD.FX", "CS.D.{pair}.CFD.IP") == "CS.D.GBPUSD.CFD.IP"


def test_pair_spec_knows_jpy_pips_and_refuses_an_unknown_pair():
    assert pair_spec("USDJPY.FX").pip_size == Decimal("0.01")
    assert pair_spec("EURUSD.FX").pip_size == Decimal("0.0001")
    # A guessed spread would decide an intraday FX result, so none is made up.
    with pytest.raises(NotAnFxSymbolError, match="explicit half-spread"):
        pair_spec("ZZZYYY.FX")


def test_calendar_for_symbol():
    assert calendar_for_symbol("EURUSD.FX") is FX_CALENDAR
    assert calendar_for_symbol("TQQQ.US") is US_EQUITY_CALENDAR


# --- the FX session clock ----------------------------------------------------


def test_sunday_evening_belongs_to_mondays_session():
    assert FX_CALENDAR.session_date(ny(2025, 3, 2, 17, 30)) == date(2025, 3, 3)
    assert FX_CALENDAR.session_date(ny(2025, 3, 3, 16, 55)) == date(2025, 3, 3)
    assert FX_CALENDAR.session_date(ny(2025, 3, 3, 17, 0)) == date(2025, 3, 4)


def test_weekend_is_closed_and_the_roll_hour_is_blacked_out():
    assert FX_CALENDAR.session_phase(ny(2025, 3, 7, 17, 0)) is None  # Friday close
    assert FX_CALENDAR.session_phase(ny(2025, 3, 8, 12, 0)) is None  # Saturday
    assert FX_CALENDAR.session_phase(ny(2025, 3, 9, 16, 55)) is None  # Sunday pre-open
    assert FX_CALENDAR.session_phase(ny(2025, 3, 9, 17, 30)) is None  # roll blackout
    assert FX_CALENDAR.session_phase(ny(2025, 3, 9, 18, 0)) is SessionPhase.PRE_MARKET


def test_london_open_starts_the_regular_phase_in_londons_own_dst():
    # 2025-03-10: New York is already on EDT, London still on GMT - the
    # London open is 04:00 NY that week, not the usual 03:00.
    london_open = datetime(2025, 3, 10, 8, 0, tzinfo=LDN)
    assert FX_CALENDAR.session_phase(london_open - timedelta(minutes=5)) is (
        SessionPhase.PRE_MARKET
    )
    assert FX_CALENDAR.session_phase(london_open) is SessionPhase.REGULAR
    assert london_open.astimezone(NY).hour == 4
    assert FX_CALENDAR.session_phase(ny(2025, 3, 10, 16, 55)) is SessionPhase.REGULAR


def test_trading_asia_makes_everything_after_the_blackout_regular():
    cal = FxCalendar(trade_asian_session=True)
    assert cal.session_phase(ny(2025, 3, 9, 18, 0)) is SessionPhase.REGULAR
    assert cal.session_phase(ny(2025, 3, 9, 17, 30)) is None


class B:
    def __init__(self, ts, price, *, high=None, low=None, volume=None):
        self.ts = ts.astimezone(UTC)
        self.open = Decimal(str(price))
        self.high = Decimal(str(high if high is not None else price))
        self.low = Decimal(str(low if low is not None else price))
        self.close = Decimal(str(price))
        self.volume = volume


def _fx_day(session_day: date, *, asia=(1.10, 1.11, 1.09), london=(1.12, 1.08)):
    """One FX session of hourly-ish bars: blackout, Asia, London+NY."""
    prev = session_day - timedelta(days=1)
    open_ = datetime(prev.year, prev.month, prev.day, 17, 0, tzinfo=NY)
    bars = [B(open_ + timedelta(minutes=10), 1.5)]  # inside the roll blackout
    bars.append(B(open_ + timedelta(hours=2), asia[0], high=asia[1], low=asia[2]))
    lo = datetime(session_day.year, session_day.month, session_day.day, 8, 0, tzinfo=LDN)
    bars.append(B(lo, 1.10, high=london[0], low=1.10))
    bars.append(B(lo + timedelta(minutes=5), 1.10, high=1.10, low=london[1]))
    bars.append(B(lo + timedelta(minutes=30), 1.10))
    bars.append(B(datetime(session_day.year, session_day.month, session_day.day, 16, 55,
                           tzinfo=NY), 1.105))
    return bars


def test_fx_levels_use_asia_as_premarket_and_anchor_the_range_at_london():
    bars = _fx_day(date(2025, 6, 3)) + _fx_day(date(2025, 6, 4))
    levels = build_session_levels(bars, calendar=FX_CALENDAR)
    assert sorted(levels) == [date(2025, 6, 3), date(2025, 6, 4)]
    day = levels[date(2025, 6, 4)]
    # The blackout print at 1.5 feeds nothing.
    assert day.premarket_high == Decimal("1.11") and day.premarket_low == Decimal("1.09")
    assert day.opening_range_high == Decimal("1.12")
    assert day.opening_range_low == Decimal("1.08")
    assert day.previous_high == Decimal("1.12") and day.previous_close == Decimal("1.105")
    assert day.opening_range_complete_at == datetime(2025, 6, 4, 8, 15, tzinfo=LDN)


def test_an_incomplete_opening_range_is_hidden_from_earlier_bars():
    levels = build_session_levels(_fx_day(date(2025, 6, 4)), calendar=FX_CALENDAR)
    day = levels[date(2025, 6, 4)]
    early = day.known_at(datetime(2025, 6, 4, 8, 5, tzinfo=LDN))
    assert early.opening_range_high is None and early.opening_range_low is None
    assert early.premarket_high == day.premarket_high
    assert day.known_at(datetime(2025, 6, 4, 8, 15, tzinfo=LDN)) is day


def test_the_equity_calendar_is_unchanged_and_tracks_no_completion_time():
    t = ny(2026, 9, 15, 9, 30)
    bars = [B(t + timedelta(minutes=5 * i), 100 + i, volume=1000) for i in range(6)]
    levels = build_session_levels(bars)[date(2026, 9, 15)]
    assert levels.opening_range_complete_at is None
    assert levels.known_at(t) is levels
    assert list(group_by_session(bars)) == [date(2026, 9, 15)]


def test_fx_bars_without_volume_produce_no_vwap():
    assert session_vwap(_fx_day(date(2025, 6, 4)), calendar=FX_CALENDAR) == []


# --- resampling --------------------------------------------------------------


def _m1(minute, o, h, low, c, volume=None):
    return Bar(
        symbol="EURUSD.FX", bar_interval="1m",
        ts=datetime(2025, 6, 4, 12, minute, tzinfo=UTC),
        open=Decimal(o), high=Decimal(h), low=Decimal(low), close=Decimal(c),
        volume=volume, source="test",
    )


def test_resample_aggregates_real_bars_only():
    bars = [
        _m1(0, "1.1", "1.2", "1.0", "1.15"),
        _m1(3, "1.15", "1.3", "1.1", "1.2"),
        # minutes 5-9 are missing entirely: no bucket is invented for them
        _m1(10, "1.2", "1.25", "1.19", "1.21"),
    ]
    out = resample_bars(bars, "5m")
    assert [b.ts.minute for b in out] == [0, 10]
    first = out[0]
    assert (first.open, first.high, first.low, first.close) == (
        Decimal("1.1"), Decimal("1.3"), Decimal("1.0"), Decimal("1.2")
    )
    assert first.volume is None and first.bar_interval == "5m"


def test_resample_refuses_a_daily_fx_bar():
    with pytest.raises(ValueError):
        resample_bars([_m1(0, "1", "1", "1", "1")], "1d")


# --- HistData reader ---------------------------------------------------------


def test_histdata_lines_are_fixed_utc_minus_5_bid_bars_without_volume():
    # A July timestamp: America/New_York would be UTC-4 here. HistData is
    # EST WITHOUT daylight saving, so this must come out at 17:00 UTC.
    bar = parse_line("20250701 120000;1.17000;1.17010;1.16990;1.17005;0", symbol="EURUSD.FX")
    assert bar.ts == datetime(2025, 7, 1, 17, 0, tzinfo=UTC)
    assert bar.volume is None
    assert bar.source == "histdata-bid"
    assert bar.close == Decimal("1.17005")
    with pytest.raises(HistDataFormatError):
        parse_line("garbage", symbol="EURUSD.FX")
