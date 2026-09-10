"""Integration tests against a real Postgres instance - MarketDataStore
exists specifically to round-trip through market_data_bars, so there's no
meaningful pure-unit version of these tests. Mirrors
tests/execution/test_persistence.py's db_session fixture shape.
"""

import contextlib
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete

from apps.api.app.db.base import get_session
from apps.api.app.db.models import MarketDataBar
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.store import MarketDataStore

TEST_SYMBOL = "TESTBAR.US"


@contextlib.asynccontextmanager
async def db_session():
    gen = get_session()
    session = await anext(gen)
    try:
        yield session
    finally:
        await gen.aclose()


@contextlib.asynccontextmanager
async def clean_bars(session, symbol: str = TEST_SYMBOL):
    try:
        yield
    finally:
        await session.execute(delete(MarketDataBar).where(MarketDataBar.symbol == symbol))
        await session.commit()


def _bar(day: int, close: str, *, symbol: str = TEST_SYMBOL) -> Bar:
    return Bar(
        symbol=symbol,
        bar_interval="1d",
        ts=datetime(2026, 8, day, 21, 0, tzinfo=UTC),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1_000,
        source="test-fixture",
    )


@pytest.mark.asyncio
async def test_upsert_then_get_bars_returns_them_oldest_first():
    async with db_session() as session, clean_bars(session):
        store = MarketDataStore(session)
        bars = [_bar(20, "101"), _bar(18, "100"), _bar(19, "100.5")]

        written = await store.upsert_bars(bars)
        await session.commit()

        assert written == 3
        read_back = await store.get_bars(
            TEST_SYMBOL,
            bar_interval="1d",
            start_date=bars[0].ts.date(),
            end_date=bars[0].ts.date(),
        )
        # Single-day query hits only the day-20 bar.
        assert [b.close for b in read_back] == [Decimal("101")]

        full_range = await store.get_bars(
            TEST_SYMBOL, bar_interval="1d", start_date=date(2026, 8, 18), end_date=date(2026, 8, 20)
        )
        assert [b.close for b in full_range] == [Decimal("100"), Decimal("100.5"), Decimal("101")]


@pytest.mark.asyncio
async def test_re_ingesting_the_same_bar_updates_it_in_place_not_a_duplicate():
    async with db_session() as session, clean_bars(session):
        store = MarketDataStore(session)
        first = _bar(20, "101")
        await store.upsert_bars([first])
        await session.commit()

        revised = first.model_copy(update={"close": Decimal("105"), "source": "test-fixture-v2"})
        await store.upsert_bars([revised])
        await session.commit()

        rows = await store.get_bars(
            TEST_SYMBOL,
            bar_interval="1d",
            start_date=first.ts.date(),
            end_date=first.ts.date(),
        )
        assert len(rows) == 1
        assert rows[0].close == Decimal("105")
        assert rows[0].source == "test-fixture-v2"


@pytest.mark.asyncio
async def test_get_bars_returns_empty_list_never_padded_when_nothing_ingested():
    async with db_session() as session, clean_bars(session):
        store = MarketDataStore(session)

        rows = await store.get_bars(
            "NOTHING-INGESTED.US",
            bar_interval="1d",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        )
        assert rows == []


@pytest.mark.asyncio
async def test_get_latest_bars_returns_the_newest_n_oldest_first():
    """The selection is newest-first (that is what "latest" means) but the
    RESULT is oldest-first, because that is what every evaluator this feeds
    expects - the same contract `get_bars` has."""
    async with db_session() as session, clean_bars(session):
        store = MarketDataStore(session)
        await store.upsert_bars(
            [_bar(day, str(100 + day)) for day in (17, 18, 19, 20, 21)]
        )
        await session.commit()

        latest = await store.get_latest_bars(TEST_SYMBOL, bar_interval="1d", count=3)

        # The three NEWEST days (19, 20, 21) - not the first three ingested.
        assert [b.ts.day for b in latest] == [19, 20, 21]
        assert [b.close for b in latest] == [Decimal(119), Decimal(120), Decimal(121)]


@pytest.mark.asyncio
async def test_get_latest_bars_returns_fewer_than_asked_never_padded():
    """Two ingested bars answer a request for ten with two rows, never with
    ten - a short series is a real data gap the caller must handle, exactly as
    `get_bars`'s own contract requires."""
    async with db_session() as session, clean_bars(session):
        store = MarketDataStore(session)
        await store.upsert_bars([_bar(18, "100"), _bar(19, "101")])
        await session.commit()

        latest = await store.get_latest_bars(TEST_SYMBOL, bar_interval="1d", count=10)

        assert [b.close for b in latest] == [Decimal(100), Decimal(101)]


@pytest.mark.asyncio
async def test_get_latest_bars_is_empty_when_nothing_is_ingested_for_that_series():
    """Nothing for the symbol at all, and nothing for the symbol at a
    DIFFERENT interval - the interval is matched exactly, never coerced, so
    ingested 1d bars are not silently returned as 1m ones."""
    async with db_session() as session, clean_bars(session):
        store = MarketDataStore(session)
        await store.upsert_bars([_bar(18, "100")])
        await session.commit()

        assert (
            await store.get_latest_bars(
                "NOTHING-INGESTED.US", bar_interval="1d", count=5
            )
            == []
        )
        assert await store.get_latest_bars(TEST_SYMBOL, bar_interval="1m", count=5) == []


@pytest.mark.asyncio
async def test_upsert_bars_with_empty_sequence_is_a_no_op():
    async with db_session() as session, clean_bars(session):
        store = MarketDataStore(session)
        assert await store.upsert_bars([]) == 0
