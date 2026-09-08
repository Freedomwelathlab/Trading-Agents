"""Integration tests against a real Postgres instance - run_backfill_job
writes both market_data_backfill_jobs and market_data_bars rows in the same
transaction, so this is exercised at the DB-backed integration level, same
as tests/execution/test_persistence.py.
"""

import contextlib
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from apps.api.app.db.base import get_session
from apps.api.app.db.models import BackfillJobStatus, MarketDataBackfillJob, MarketDataBar
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.ingestion.backfill import run_backfill_job
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError

TEST_SYMBOL = "TESTBACKFILL.US"


@contextlib.asynccontextmanager
async def db_session():
    gen = get_session()
    session = await anext(gen)
    try:
        yield session
    finally:
        await gen.aclose()


@contextlib.asynccontextmanager
async def clean_up(session, symbol: str = TEST_SYMBOL):
    try:
        yield
    finally:
        await session.rollback()
        await session.execute(delete(MarketDataBar).where(MarketDataBar.symbol == symbol))
        await session.execute(
            delete(MarketDataBackfillJob).where(MarketDataBackfillJob.symbol == symbol)
        )
        await session.commit()


class FakeBarBackfillProvider:
    name = "fake-backfill"

    def __init__(self, *, bars: list[Bar] | None = None, error: Exception | None = None):
        self._bars = bars
        self._error = error
        self.calls: list[tuple] = []

    async def get_bars(self, symbol, *, bar_interval, start_date, end_date):
        self.calls.append((symbol, bar_interval, start_date, end_date))
        if self._error is not None:
            raise self._error
        assert self._bars is not None
        return self._bars


def _bar(day: int, close: str) -> Bar:
    return Bar(
        symbol=TEST_SYMBOL,
        bar_interval="1d",
        ts=datetime(2026, 8, day, 21, 0, tzinfo=UTC),
        close=Decimal(close),
        source="fake-backfill",
    )


@pytest.mark.asyncio
async def test_a_successful_backfill_ingests_bars_and_records_a_succeeded_job():
    async with db_session() as session, clean_up(session):
        provider = FakeBarBackfillProvider(bars=[_bar(18, "100"), _bar(20, "102")])

        job = await run_backfill_job(
            session,
            symbol=TEST_SYMBOL,
            bar_interval="1d",
            start_date=date(2026, 8, 18),
            end_date=date(2026, 8, 20),
            provider=provider,
            requested_by_user_id=None,
        )

        assert job.status is BackfillJobStatus.SUCCEEDED
        assert job.bars_ingested == 2
        assert job.earliest_bar_date == date(2026, 8, 18)
        assert job.latest_bar_date == date(2026, 8, 20)
        assert job.error_detail is None

        rows = (
            (
                await session.execute(
                    select(MarketDataBar).where(MarketDataBar.symbol == TEST_SYMBOL)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert provider.calls == [(TEST_SYMBOL, "1d", date(2026, 8, 18), date(2026, 8, 20))]


@pytest.mark.asyncio
async def test_a_vendor_failure_is_recorded_as_failed_with_the_real_error_never_a_fabricated_bar():
    async with db_session() as session, clean_up(session):
        provider = FakeBarBackfillProvider(error=VendorError("rate limited"))

        job = await run_backfill_job(
            session,
            symbol=TEST_SYMBOL,
            bar_interval="1d",
            start_date=date(2026, 8, 18),
            end_date=date(2026, 8, 20),
            provider=provider,
            requested_by_user_id=None,
        )

        assert job.status is BackfillJobStatus.FAILED
        assert job.bars_ingested == 0
        assert "rate limited" in job.error_detail

        rows = (
            (
                await session.execute(
                    select(MarketDataBar).where(MarketDataBar.symbol == TEST_SYMBOL)
                )
            )
            .scalars()
            .all()
        )
        assert rows == []


@pytest.mark.asyncio
async def test_a_data_unavailable_vendor_response_is_recorded_as_failed_not_a_silent_zero():
    async with db_session() as session, clean_up(session):
        provider = FakeBarBackfillProvider(
            error=DataUnavailableError(f"Longbridge returned no candlesticks for {TEST_SYMBOL!r}.")
        )

        job = await run_backfill_job(
            session,
            symbol=TEST_SYMBOL,
            bar_interval="1d",
            start_date=date(2026, 8, 18),
            end_date=date(2026, 8, 20),
            provider=provider,
            requested_by_user_id=None,
        )

        assert job.status is BackfillJobStatus.FAILED
        assert TEST_SYMBOL in job.error_detail


@pytest.mark.asyncio
async def test_re_running_a_backfill_over_an_overlapping_window_does_not_duplicate_rows():
    async with db_session() as session, clean_up(session):
        provider = FakeBarBackfillProvider(bars=[_bar(18, "100"), _bar(19, "101")])
        await run_backfill_job(
            session,
            symbol=TEST_SYMBOL,
            bar_interval="1d",
            start_date=date(2026, 8, 18),
            end_date=date(2026, 8, 19),
            provider=provider,
            requested_by_user_id=None,
        )

        overlapping_provider = FakeBarBackfillProvider(bars=[_bar(19, "105"), _bar(20, "106")])
        job = await run_backfill_job(
            session,
            symbol=TEST_SYMBOL,
            bar_interval="1d",
            start_date=date(2026, 8, 19),
            end_date=date(2026, 8, 20),
            provider=overlapping_provider,
            requested_by_user_id=None,
        )
        assert job.status is BackfillJobStatus.SUCCEEDED

        rows = (
            (
                await session.execute(
                    select(MarketDataBar)
                    .where(MarketDataBar.symbol == TEST_SYMBOL)
                    .order_by(MarketDataBar.ts.asc())
                )
            )
            .scalars()
            .all()
        )
        # 3 distinct days (18, 19, 20), day 19 overwritten with the second
        # run's close - never a duplicate row for the same (symbol,
        # bar_interval, ts).
        assert len(rows) == 3
        assert rows[1].close == Decimal("105")
