"""The Autotrade bot's `24h` market type (Phase 106, D130).

Crypto never closes and FX closes only for the weekend, so a bot set to 24
hours trades each symbol on its own clock: crypto every hour of every day
and never forced flat, FX Sunday 17:00 NY to Friday 17:00 NY, and US
equities the extended day exactly as `auto` does.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apps.api.app.autotrade.engine import bot_calendar, phase_allowed, session_ending
from apps.api.app.autotrade.scanner import scan_latest_bar
from apps.api.app.autotrade.service import MARKET_TYPES
from apps.api.app.backtesting.brackets import BracketPlan
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.sessions import CRYPTO_24H_CALENDAR, FX_24H_CALENDAR, SessionPhase

SAT_3AM_UTC = datetime(2026, 9, 26, 3, 0, tzinfo=UTC)  # a Saturday
WED_2AM_UTC = datetime(2026, 9, 23, 2, 0, tzinfo=UTC)  # Tue 22:00 NY


def test_24h_is_a_market_type_that_fits_the_column():
    assert "24h" in MARKET_TYPES and all(len(m) <= 16 for m in MARKET_TYPES)


def test_each_symbol_gets_its_own_clock():
    assert bot_calendar("BTC-USD", "24h") is CRYPTO_24H_CALENDAR
    assert bot_calendar("ETH/USD", "24h") is CRYPTO_24H_CALENDAR
    assert bot_calendar("EURUSD.FX", "24h") is FX_24H_CALENDAR
    assert bot_calendar("TQQQ.US", "24h") is None
    assert bot_calendar("BTC-USD", "auto") is None  # only 24h changes anything


def test_crypto_trades_on_a_saturday_night_equities_do_not():
    assert phase_allowed(SAT_3AM_UTC, "24h", "BTC-USD")
    assert not phase_allowed(SAT_3AM_UTC, "24h", "EURUSD.FX")  # FX weekend
    assert not phase_allowed(SAT_3AM_UTC, "24h", "TQQQ.US")
    assert not phase_allowed(SAT_3AM_UTC, "auto", "BTC-USD")  # auto keeps equity hours


def test_fx_trades_through_the_night_with_no_daily_rollover_gap():
    assert phase_allowed(WED_2AM_UTC, "24h", "EURUSD.FX")
    # 17:05 NY on a Wednesday: inside the default FX calendar's blackout,
    # but a 24h bot has none.
    assert FX_24H_CALENDAR.session_phase(datetime(2026, 9, 23, 21, 5, tzinfo=UTC)) is not None


def test_a_crypto_position_is_never_forced_flat_fx_only_before_the_weekend():
    for hour in range(24):
        ts = SAT_3AM_UTC + timedelta(hours=hour)
        assert not session_ending(ts, market_type="24h", bar_interval="5m", symbol="BTC-USD")
    # Friday 16:55 NY is the last FX bar of the week.
    fri_close = datetime(2026, 9, 25, 20, 55, tzinfo=UTC)
    assert session_ending(fri_close, market_type="24h", bar_interval="5m", symbol="EURUSD.FX")
    assert not session_ending(WED_2AM_UTC, market_type="24h", bar_interval="5m",
                              symbol="EURUSD.FX")


def _bars(start: datetime, n: int) -> list[Bar]:
    out = []
    for k in range(n):
        px = Decimal("100") + Decimal(k % 7)
        out.append(Bar(symbol="BTC-USD", bar_interval="5m", ts=start + timedelta(minutes=5 * k),
                       open=px, high=px + 1, low=px - 1, close=px, volume=10, source="test"))
    return out


def test_the_scanner_evaluates_a_crypto_bar_at_3am_on_a_saturday():
    bars = _bars(SAT_3AM_UTC - timedelta(hours=2, minutes=55), 36)  # 00:05..03:00 UTC Sat
    on_equity_clock = scan_latest_bar(
        "BTC-USD", bars, setups=["orb_failure"], market_type="24h", min_score=0,
        plan=BracketPlan(),
    )
    assert on_equity_clock.reason.startswith("phase_")  # the old behaviour: closed
    on_own_clock = scan_latest_bar(
        "BTC-USD", bars, setups=["orb_failure"], market_type="24h", min_score=0,
        plan=BracketPlan(), calendar=CRYPTO_24H_CALENDAR,
    )
    # Past the phase gate: whatever the setup decides, it was evaluated.
    assert not on_own_clock.reason.startswith("phase_")
    assert on_own_clock.hit is None or on_own_clock.hit.phase is SessionPhase.REGULAR
