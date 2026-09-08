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
async def test_upsert_bars_with_empty_sequence_is_a_no_op():
    async with db_session() as session, clean_bars(session):
        store = MarketDataStore(session)
        assert await store.upsert_bars([]) == 0
