"""Unit tests for the snapshot scheduler's market-hours gate (Phase 35,
docs/DECISIONS.md D042).

`MarketHoursGate.evaluate()` does no I/O and reads no clock, so every case
here is exact: a real instant in, a typed decision out. Nothing is mocked
because there is nothing to mock.

Note what these tests deliberately do NOT assert: that any particular
holiday is closed, or that any particular time of day is inside a session.
The gate makes no such claim (see apps/api/app/portfolio/market_hours.py),
and a test asserting it would be asserting a fabrication.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from apps.api.app.core.config import Settings
from apps.api.app.portfolio.market_hours import (
    MarketHoursDecision,
    MarketHoursGate,
    utc_now,
)

# 2026-08-31 is a Monday, so this block spans one full, unambiguous week.
MONDAY = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
WEEK = {
    "monday": MONDAY,
    "tuesday": MONDAY + timedelta(days=1),
    "wednesday": MONDAY + timedelta(days=2),
    "thursday": MONDAY + timedelta(days=3),
    "friday": MONDAY + timedelta(days=4),
    "saturday": MONDAY + timedelta(days=5),
    "sunday": MONDAY + timedelta(days=6),
}


def test_the_week_fixture_really_is_the_days_it_claims():
    """Guards the rest of this file: every assertion below is meaningless if
    these constants drift off the real calendar."""
    for index, (name, moment) in enumerate(WEEK.items()):
        assert moment.strftime("%A").lower() == name
        assert moment.weekday() == index


@pytest.mark.parametrize("day", ["monday", "tuesday", "wednesday", "thursday", "friday"])
def test_weekdays_run(day: str):
    assert MarketHoursGate().evaluate(WEEK[day]) is MarketHoursDecision.RUN


@pytest.mark.parametrize("day", ["saturday", "sunday"])
def test_weekends_are_skipped(day: str):
    decision = MarketHoursGate().evaluate(WEEK[day])

    assert decision is MarketHoursDecision.SKIP_WEEKEND
    assert decision.should_run is False


@pytest.mark.parametrize("day", list(WEEK))
def test_a_disabled_gate_runs_every_day_including_the_weekend(day: str):
    """The escape hatch: someone testing, or snapshotting a 24/7 instrument,
    must be able to get D030's unconditional behavior back."""
    decision = MarketHoursGate(enabled=False).evaluate(WEEK[day])

    assert decision is MarketHoursDecision.RUN_GATE_DISABLED
    assert decision.should_run is True


def test_a_disabled_gate_is_reported_distinctly_from_a_weekday():
    """Both run, but the logs must be able to tell "it was Tuesday" from
    "nobody was checking"."""
    assert MarketHoursGate(enabled=True).evaluate(WEEK["tuesday"]) is MarketHoursDecision.RUN
    assert (
        MarketHoursGate(enabled=False).evaluate(WEEK["tuesday"])
        is MarketHoursDecision.RUN_GATE_DISABLED
    )


def test_the_gate_is_enabled_by_default():
    assert MarketHoursGate().enabled is True


def test_a_naive_datetime_is_rejected_rather_than_assumed_to_be_utc():
    """An assumed offset can move an instant across a day boundary and so
    flip the weekend answer - exactly the kind of silent guess this codebase
    refuses everywhere else."""
    with pytest.raises(ValueError, match="timezone-aware"):
        MarketHoursGate().evaluate(datetime(2026, 8, 31, 12, 0))


def test_a_non_utc_instant_is_converted_not_read_off_its_local_calendar():
    """Saturday 01:00 in UTC+9 is Friday 16:00 UTC - a real, open US
    session. The gate must run it, which it only does if it converts rather
    than reading `.weekday()` off the local wall clock."""
    tokyo = timezone(timedelta(hours=9))
    saturday_local = datetime(2026, 9, 5, 1, 0, tzinfo=tokyo)

    assert saturday_local.weekday() == 5  # locally a Saturday
    assert saturday_local.astimezone(UTC).weekday() == 4  # really a UTC Friday
    assert MarketHoursGate().evaluate(saturday_local) is MarketHoursDecision.RUN


def test_the_converse_direction_is_also_handled():
    """Friday 22:00 in UTC-5 is Saturday 03:00 UTC, inside the skipped
    window."""
    new_york_ish = timezone(timedelta(hours=-5))
    friday_local = datetime(2026, 9, 4, 22, 0, tzinfo=new_york_ish)

    assert friday_local.weekday() == 4
    assert MarketHoursGate().evaluate(friday_local) is MarketHoursDecision.SKIP_WEEKEND


def test_the_boundary_instants_of_the_weekend_window():
    """The gate skips exactly [Sat 00:00 UTC, Mon 00:00 UTC)."""
    gate = MarketHoursGate()
    one_us = timedelta(microseconds=1)

    saturday_start = datetime(2026, 9, 5, 0, 0, tzinfo=UTC)
    monday_start = datetime(2026, 9, 7, 0, 0, tzinfo=UTC)

    assert gate.evaluate(saturday_start - one_us) is MarketHoursDecision.RUN
    assert gate.evaluate(saturday_start) is MarketHoursDecision.SKIP_WEEKEND
    assert gate.evaluate(monday_start - one_us) is MarketHoursDecision.SKIP_WEEKEND
    assert gate.evaluate(monday_start) is MarketHoursDecision.RUN


def test_the_gate_never_suppresses_a_regular_session_of_a_supported_market():
    """The claim market_hours.py's docstring makes, checked rather than
    asserted in prose: the earliest regular open (HK/CN 09:30 UTC+8 on a
    Monday) and the latest regular close (US 16:00 ET on a Friday, in both
    DST states) all land on UTC weekdays."""
    gate = MarketHoursGate()

    hk_cn = timezone(timedelta(hours=8))
    monday_open_hk = datetime(2026, 9, 7, 9, 30, tzinfo=hk_cn)
    assert gate.evaluate(monday_open_hk) is MarketHoursDecision.RUN

    for offset_hours in (-4, -5):  # US Eastern, DST and standard
        friday_close_us = datetime(
            2026, 9, 4, 16, 0, tzinfo=timezone(timedelta(hours=offset_hours))
        )
        assert gate.evaluate(friday_close_us) is MarketHoursDecision.RUN


def test_us_extended_hours_late_on_a_friday_is_the_one_knowingly_clipped_window():
    """Documented in market_hours.py as accepted, not overlooked. Pinned by
    a test so it stays a deliberate tradeoff rather than becoming a
    forgotten one."""
    us_eastern_dst = timezone(timedelta(hours=-4))
    friday_post_market = datetime(2026, 9, 4, 20, 0, tzinfo=us_eastern_dst)

    assert friday_post_market.astimezone(UTC).weekday() == 5
    assert MarketHoursGate().evaluate(friday_post_market) is MarketHoursDecision.SKIP_WEEKEND


def test_utc_now_returns_an_aware_utc_instant_the_gate_accepts():
    """The default clock and the gate's input contract must agree, or the
    scheduler would raise on its very first real cycle."""
    now = utc_now()

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    assert MarketHoursGate().evaluate(now) in set(MarketHoursDecision)


def test_the_market_hours_gate_defaults_to_enabled_in_settings():
    """Unlike the scheduler switch itself (fail-closed at false), the safer
    default here is ON, because ON is the side that does less work."""
    settings = Settings(jwt_secret_key="x" * 32)

    assert settings.portfolio_snapshot_market_hours_gate_enabled is True


def test_the_market_hours_gate_can_be_switched_off_by_configuration():
    settings = Settings(
        jwt_secret_key="x" * 32, portfolio_snapshot_market_hours_gate_enabled=False
    )

    assert settings.portfolio_snapshot_market_hours_gate_enabled is False
