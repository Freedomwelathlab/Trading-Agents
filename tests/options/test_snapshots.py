"""Option chain snapshots and their daily loop (Phase 100, D119).

Integration tests against the real Postgres: the rules under test are
about what reaches the table - the DTE and strike filters that keep it
small, one snapshot per trading day, NULL rather than zero for a missing
field, and a stale feed writing nothing. The only double is the chain
vendor (no network).
"""

import contextlib
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, text

from apps.api.app.db.base import get_session, get_session_factory
from apps.api.app.db.models import OptionChainSnapshot
from apps.api.app.marketdata.option_chain_provider import OptionChain, OptionQuote
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.options.pricing import OptionRight
from apps.api.app.options.snapshot_scheduler import (
    OPTION_SNAPSHOT_LOCK_OBJID,
    OptionSnapshotOutcome,
    build_option_snapshot_cycle_lock,
    capture_due,
    run_option_snapshot_cycle,
)
from apps.api.app.options.snapshots import (
    CaptureStatus,
    capture_chain_snapshot,
    list_snapshot_dates,
    load_snapshot,
)
from apps.api.app.portfolio.cycle_lock import SNAPSHOT_LOCK_CLASSID

UNDERLYING = "ZZSNAP.US"
ROOT = "ZZSNAP"
FRIDAY_CLOSE = datetime(2026, 9, 25, 19, 59, 59, tzinfo=UTC)  # 15:59:59 EDT
MONDAY_NIGHT = datetime(2026, 9, 28, 6, 0, tzinfo=UTC)          # 02:00 EDT Monday
IN_RANGE = [date(2026, 10, 2), date(2026, 10, 16), date(2026, 11, 20)]
TOO_FAR = date(2026, 12, 18)  # 81 days from 2026-09-28
STRIKES = [50, 60, 70, 80, 90, 100, 110]  # spot 80: 56..104 kept


def occ(expiry: date, right: OptionRight, strike: int) -> str:
    return f"{ROOT}{expiry:%y%m%d}{'C' if right is OptionRight.CALL else 'P'}{strike * 1000:08d}"


class StubProvider:
    name = "stub-delayed"

    def __init__(self, *, as_of=FRIDAY_CLOSE, spot=Decimal("80"), expiries=None, fail=None):
        self.as_of = as_of
        self.spot = spot
        self.expiries = IN_RANGE + [TOO_FAR] if expiries is None else expiries
        self.fail = fail
        self.chain_calls = 0

    async def get_expiries(self, underlying):
        if self.fail:
            raise self.fail
        return list(self.expiries)

    async def get_underlying_price(self, underlying):
        return self.spot

    async def get_chain(self, underlying, expiry):
        self.chain_calls += 1
        quotes = []
        for k in STRIKES:
            for right in (OptionRight.CALL, OptionRight.PUT):
                quotes.append(
                    OptionQuote(
                        contract_symbol=occ(expiry, right, k),
                        underlying=underlying,
                        expiry=expiry,
                        strike=Decimal(k),
                        right=right,
                        bid=Decimal("1.00") if k != 90 else None,
                        ask=Decimal("1.20"),
                        implied_vol=Decimal("0.55"),
                        delta=Decimal("0.5"),
                        open_interest=10,
                    )
                )
        return OptionChain(underlying, expiry, self.as_of, self.name, tuple(quotes))


class NoSpotProvider(StubProvider):
    async def get_underlying_price(self, underlying):
        return None


@contextlib.asynccontextmanager
async def session_scope():
    gen = get_session()
    session = await anext(gen)
    try:
        yield session
    finally:
        await session.rollback()
        await session.execute(
            delete(OptionChainSnapshot).where(OptionChainSnapshot.underlying == UNDERLYING)
        )
        await session.commit()
        await gen.aclose()


async def _count(session) -> int:
    return await session.scalar(
        select(func.count()).where(OptionChainSnapshot.underlying == UNDERLYING)
    )


@pytest.mark.asyncio
async def test_capture_keeps_only_near_expiries_and_strikes_near_spot():
    async with session_scope() as session:
        result = await capture_chain_snapshot(
            session, StubProvider(), UNDERLYING, now=MONDAY_NIGHT
        )
        await session.commit()
        assert result.status is CaptureStatus.CAPTURED
        assert result.expiries == tuple(IN_RANGE)  # 81-DTE expiry never fetched
        assert result.rows_written == 3 * 5 * 2
        rows = await load_snapshot(session, UNDERLYING)
        assert len(rows) == 30
        assert {r.strike for r in rows} == {Decimal(k) for k in (60, 70, 80, 90, 100)}
        # The vendor's day, not the reading day.
        assert {r.trade_date for r in rows} == {date(2026, 9, 25)}
        assert rows[0].source == "stub-delayed"
        assert rows[0].underlying_price == Decimal("80")


@pytest.mark.asyncio
async def test_a_missing_bid_is_stored_as_null_not_zero():
    async with session_scope() as session:
        await capture_chain_snapshot(session, StubProvider(), UNDERLYING, now=MONDAY_NIGHT)
        await session.commit()
        rows = await load_snapshot(session, UNDERLYING, date(2026, 9, 25))
        at_90 = [r for r in rows if r.strike == Decimal(90)]
        assert at_90 and all(r.bid is None for r in at_90)
        assert all(r.ask == Decimal("1.2") for r in at_90)
        assert all(r.volume is None for r in rows)


@pytest.mark.asyncio
async def test_a_second_capture_for_the_same_trading_day_replaces_the_first():
    async with session_scope() as session:
        await capture_chain_snapshot(session, StubProvider(), UNDERLYING, now=MONDAY_NIGHT)
        await session.commit()
        later = StubProvider(as_of=datetime(2026, 9, 25, 20, 15, tzinfo=UTC))
        second = await capture_chain_snapshot(session, later, UNDERLYING, now=MONDAY_NIGHT)
        await session.commit()
        assert second.rows_replaced == 30
        assert await _count(session) == 30
        assert await list_snapshot_dates(session, UNDERLYING) == [date(2026, 9, 25)]
        rows = await load_snapshot(session, UNDERLYING)
        assert {r.as_of for r in rows} == {datetime(2026, 9, 25, 20, 15, tzinfo=UTC)}


@pytest.mark.asyncio
async def test_quotes_for_another_day_are_not_stored_when_today_is_required():
    async with session_scope() as session:
        result = await capture_chain_snapshot(
            session, StubProvider(), UNDERLYING, now=MONDAY_NIGHT,
            require_trade_date=date(2026, 9, 28),
        )
        assert result.status is CaptureStatus.STALE_SOURCE
        assert result.rows_written == 0
        assert await _count(session) == 0


@pytest.mark.asyncio
async def test_no_spot_anywhere_is_data_unavailable_not_a_guessed_band():
    async with session_scope() as session:
        with pytest.raises(DataUnavailableError):
            await capture_chain_snapshot(
                session, NoSpotProvider(), UNDERLYING, now=MONDAY_NIGHT
            )
        assert await _count(session) == 0


@pytest.mark.asyncio
async def test_no_expiry_in_range_is_data_unavailable():
    async with session_scope() as session:
        with pytest.raises(DataUnavailableError):
            await capture_chain_snapshot(
                session, StubProvider(expiries=[TOO_FAR]), UNDERLYING, now=MONDAY_NIGHT
            )


@pytest.mark.asyncio
async def test_a_date_with_no_snapshot_reads_as_empty_not_the_nearest_day():
    async with session_scope() as session:
        await capture_chain_snapshot(session, StubProvider(), UNDERLYING, now=MONDAY_NIGHT)
        await session.commit()
        assert await load_snapshot(session, UNDERLYING, date(2026, 9, 24)) == []


# --- the scheduler -----------------------------------------------------------------------

AFTER_CUTOFF = time(16, 30)
FRIDAY_EVENING = datetime(2026, 9, 25, 21, 0, tzinfo=UTC)  # 17:00 EDT


def test_capture_is_due_after_the_new_york_cutoff_on_weekdays_only():
    assert capture_due(FRIDAY_EVENING, AFTER_CUTOFF) is None
    assert capture_due(datetime(2026, 9, 25, 20, 0, tzinfo=UTC), AFTER_CUTOFF) is (
        OptionSnapshotOutcome.SKIPPED_NOT_YET
    )
    assert capture_due(datetime(2026, 9, 26, 21, 0, tzinfo=UTC), AFTER_CUTOFF) is (
        OptionSnapshotOutcome.SKIPPED_WEEKEND
    )


def test_twenty_thirty_utc_is_before_the_close_in_winter():
    """Why the cut-off is New York time: 20:45 UTC is 15:45 EST in January,
    before the 16:00 close, and must not count as after the close."""
    january = datetime(2026, 1, 16, 20, 45, tzinfo=UTC)  # a Friday
    assert capture_due(january, AFTER_CUTOFF) is OptionSnapshotOutcome.SKIPPED_NOT_YET


async def _cleanup() -> None:
    async with get_session_factory()() as s:
        await s.execute(
            delete(OptionChainSnapshot).where(OptionChainSnapshot.underlying == UNDERLYING)
        )
        await s.commit()


@pytest.mark.asyncio
async def test_the_cycle_captures_once_per_day_after_the_close():
    provider = StubProvider()
    try:
        first = await run_option_snapshot_cycle(
            get_session_factory(), provider=provider, underlyings=[UNDERLYING],
            capture_after=AFTER_CUTOFF, clock=lambda: FRIDAY_EVENING,
        )
        assert first.outcome is OptionSnapshotOutcome.CAPTURED
        assert first.per_underlying[0].rows_written == 30
        calls = provider.chain_calls
        again = await run_option_snapshot_cycle(
            get_session_factory(), provider=provider, underlyings=[UNDERLYING],
            capture_after=AFTER_CUTOFF, clock=lambda: FRIDAY_EVENING,
        )
        assert again.outcome is OptionSnapshotOutcome.SKIPPED_ALREADY_CAPTURED
        assert provider.chain_calls == calls  # the vendor was not asked again
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_an_intraday_capture_does_not_satisfy_the_day():
    provider = StubProvider()
    try:
        async with get_session_factory()() as s:
            await capture_chain_snapshot(
                s, provider, UNDERLYING, now=datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
            )
            await s.commit()
        result = await run_option_snapshot_cycle(
            get_session_factory(), provider=provider, underlyings=[UNDERLYING],
            capture_after=AFTER_CUTOFF, clock=lambda: FRIDAY_EVENING,
        )
        assert result.outcome is OptionSnapshotOutcome.CAPTURED
        async with get_session_factory()() as s:
            rows = await load_snapshot(s, UNDERLYING, date(2026, 9, 25))
            assert len(rows) == 30
            assert {r.captured_at for r in rows} == {FRIDAY_EVENING}
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_a_holiday_feed_showing_yesterday_writes_nothing():
    provider = StubProvider(as_of=datetime(2026, 9, 24, 19, 59, tzinfo=UTC))
    try:
        result = await run_option_snapshot_cycle(
            get_session_factory(), provider=provider, underlyings=[UNDERLYING],
            capture_after=AFTER_CUTOFF, clock=lambda: FRIDAY_EVENING,
        )
        assert result.outcome is OptionSnapshotOutcome.SKIPPED_STALE_SOURCE
        async with get_session_factory()() as s:
            assert await _count(s) == 0
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_a_vendor_failure_is_reported_and_does_not_raise():
    result = await run_option_snapshot_cycle(
        get_session_factory(), provider=StubProvider(fail=VendorError("boom")),
        underlyings=[UNDERLYING], capture_after=AFTER_CUTOFF, clock=lambda: FRIDAY_EVENING,
    )
    assert result.outcome is OptionSnapshotOutcome.FAILED
    assert "boom" in (result.per_underlying[0].detail or "")


@pytest.mark.asyncio
async def test_no_provider_is_not_configured():
    result = await run_option_snapshot_cycle(
        get_session_factory(), provider=None, underlyings=[UNDERLYING],
        capture_after=AFTER_CUTOFF, clock=lambda: FRIDAY_EVENING,
    )
    assert result.outcome is OptionSnapshotOutcome.SKIPPED_NOT_CONFIGURED


@pytest.mark.asyncio
async def test_another_worker_holding_the_lock_means_this_one_does_nothing():
    provider = StubProvider()
    factory = get_session_factory()
    async with factory() as other:
        got = await other.scalar(
            text("SELECT pg_try_advisory_lock(:c, :o)"),
            {"c": SNAPSHOT_LOCK_CLASSID, "o": OPTION_SNAPSHOT_LOCK_OBJID},
        )
        assert got
        try:
            result = await run_option_snapshot_cycle(
                factory, provider=provider, underlyings=[UNDERLYING],
                capture_after=AFTER_CUTOFF, clock=lambda: FRIDAY_EVENING,
                cycle_lock=build_option_snapshot_cycle_lock(),
            )
        finally:
            await other.scalar(
                text("SELECT pg_advisory_unlock(:c, :o)"),
                {"c": SNAPSHOT_LOCK_CLASSID, "o": OPTION_SNAPSHOT_LOCK_OBJID},
            )
    assert result.outcome is OptionSnapshotOutcome.SKIPPED_LOCK_HELD
    assert provider.chain_calls == 0


def test_the_scheduler_is_off_by_default():
    from apps.api.app.core.config import Settings

    s = Settings(jwt_secret_key="x" * 32, _env_file=None)  # type: ignore[call-arg]
    assert s.option_snapshot_scheduler_enabled is False
    assert s.option_snapshot_underlying_list == ["TQQQ.US"]
    assert s.option_snapshot_capture_after_et == "16:30"
    with pytest.raises(ValueError):
        Settings(jwt_secret_key="x" * 32, option_snapshot_capture_after_et="4pm",  # type: ignore[call-arg]
                 _env_file=None)
