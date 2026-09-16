"""Integration tests for GET /market-data/{symbol}/bars (Phase 70, D088),
against a real Postgres instance.

The endpoint behind the candlestick chart. Its whole job is to hand back
what `market_data_bars` actually holds - so what is asserted here is mostly
the boundaries: an un-ingested symbol, a close-only record, an interval
outside the vocabulary, and the fact that reading never becomes writing.

Fixtures are reused from tests/api/test_admin.py rather than duplicated,
matching test_admin_market_data.py beside this file. They are async context
managers, not pytest fixtures, which is this suite's existing convention.
"""

import contextlib
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete

from apps.api.app.db.models import MarketDataBar
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.store import MarketDataStore
from tests.api.test_admin import _get_token, api_client, db_session, non_admin_user

TEST_SYMBOL = "TESTBARSAPI.US"

START = "2026-08-01"
END = "2026-08-31"


@contextlib.asynccontextmanager
async def clean_up(session, symbol: str = TEST_SYMBOL):
    try:
        yield
    finally:
        await session.rollback()
        await session.execute(delete(MarketDataBar).where(MarketDataBar.symbol == symbol))
        await session.commit()


def _bar(day: int, close: str, *, ohlc: bool = True, interval: str = "1d") -> Bar:
    return Bar(
        symbol=TEST_SYMBOL,
        bar_interval=interval,
        ts=datetime(2026, 8, day, 21, 0, tzinfo=UTC),
        open=Decimal(close) if ohlc else None,
        high=Decimal(close) + 1 if ohlc else None,
        low=Decimal(close) - 1 if ohlc else None,
        close=Decimal(close),
        volume=1_000 if ohlc else None,
        source="test-bars-api",
    )


async def _fetch(client, token: str, **overrides):
    params = {"start_date": START, "end_date": END, **overrides}
    return await client.get(
        f"/market-data/{TEST_SYMBOL}/bars",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
async def test_returns_persisted_bars_oldest_first() -> None:
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with clean_up(session):
            await MarketDataStore(session).upsert_bars(
                [_bar(25, "102"), _bar(23, "100"), _bar(24, "101")]
            )
            await session.commit()

            async with api_client() as client:
                token = await _get_token(client, email)
                res = await _fetch(client, token)

            assert res.status_code == 200, res.text
            body = res.json()
            assert body["symbol"] == TEST_SYMBOL
            assert body["bar_interval"] == "1d"
            assert body["count"] == 3
            # Oldest first, matching HistoricalBarProvider's contract - a
            # chart library misrenders an unsorted series without complaining.
            assert [b["close"] for b in body["bars"]] == [
                "100.000000",
                "101.000000",
                "102.000000",
            ]


@pytest.mark.asyncio
async def test_an_un_ingested_symbol_is_an_empty_200_not_a_404() -> None:
    """A symbol may be perfectly valid and simply un-backfilled. Those are
    different problems with different fixes, and a 404 would conflate them -
    it would say "no such symbol" about one that exists."""
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with clean_up(session):
            async with api_client() as client:
                token = await _get_token(client, email)
                res = await _fetch(client, token)

            assert res.status_code == 200
            assert res.json() == {
                "symbol": TEST_SYMBOL,
                "bar_interval": "1d",
                "count": 0,
                "bars": [],
            }


@pytest.mark.asyncio
async def test_a_close_only_bar_keeps_its_nulls_rather_than_being_completed() -> None:
    """`open`/`high`/`low` are nullable columns. Passing a null through is
    what lets the chart decline to draw a candle there; filling them from
    the close would hand the client fabricated market structure it has no
    way to identify as such."""
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with clean_up(session):
            await MarketDataStore(session).upsert_bars([_bar(23, "100", ohlc=False)])
            await session.commit()

            async with api_client() as client:
                token = await _get_token(client, email)
                res = await _fetch(client, token)

            assert res.status_code == 200
            bar = res.json()["bars"][0]
            assert bar["open"] is None
            assert bar["high"] is None
            assert bar["low"] is None
            assert bar["close"] == "100.000000"
            # Provenance travels with the bar, so a suspicious value on a
            # chart can be traced to the vendor that produced it.
            assert bar["source"] == "test-bars-api"


@pytest.mark.asyncio
async def test_bar_interval_is_matched_exactly_never_coerced() -> None:
    """Two intervals for one symbol must not bleed into each other. The
    column is a plain string, so this exactness is the whole mechanism
    keeping "the 1d series" and "the 1h series" apart - and the reason the
    request vocabulary is closed."""
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with clean_up(session):
            await MarketDataStore(session).upsert_bars(
                [_bar(23, "100"), _bar(24, "111", interval="1h")]
            )
            await session.commit()

            async with api_client() as client:
                token = await _get_token(client, email)
                daily = (await _fetch(client, token, bar_interval="1d")).json()
                hourly = (await _fetch(client, token, bar_interval="1h")).json()

            assert [b["close"] for b in daily["bars"]] == ["100.000000"]
            assert [b["close"] for b in hourly["bars"]] == ["111.000000"]


@pytest.mark.asyncio
async def test_an_interval_outside_the_vocabulary_is_a_422() -> None:
    """Without the closed vocabulary, "1 day" and "1D" would be accepted as
    intervals that never match "1d" on read."""
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with clean_up(session):
            async with api_client() as client:
                token = await _get_token(client, email)
                res = await _fetch(client, token, bar_interval="1 day")

            assert res.status_code == 422


@pytest.mark.asyncio
async def test_an_inverted_window_is_refused_rather_than_returning_nothing() -> None:
    """An end before a start would otherwise return a perfectly ordinary
    empty list, which reads as "no bars here" when the truth is "that is not
    a window"."""
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with clean_up(session):
            async with api_client() as client:
                token = await _get_token(client, email)
                res = await _fetch(client, token, start_date=END, end_date=START)

            assert res.status_code == 422
            assert "before start_date" in res.json()["detail"]


@pytest.mark.asyncio
async def test_it_requires_authentication() -> None:
    async with api_client() as client:
        res = await client.get(
            f"/market-data/{TEST_SYMBOL}/bars",
            params={"start_date": START, "end_date": END},
        )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_an_ordinary_user_may_read_bars_without_the_admin_permission() -> None:
    """Deliberately NOT admin-gated, matching `GET /{symbol}/quote` beside
    it: this returns historical data the account already holds, which is not
    privileged relative to a live quote. Ingestion is the privileged action,
    and it is a different endpoint."""
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with clean_up(session):
            async with api_client() as client:
                token = await _get_token(client, email)
                res = await _fetch(client, token)

            assert res.status_code == 200


@pytest.mark.asyncio
async def test_reading_never_ingests() -> None:
    """Reading is a read. If a chart page load could trigger a backfill it
    would spend vendor quota and write rows, so this asserts the store is
    untouched by a request that found nothing."""
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with clean_up(session):
            async with api_client() as client:
                token = await _get_token(client, email)
                assert (await _fetch(client, token)).status_code == 200

            stored = await MarketDataStore(session).get_bars(
                TEST_SYMBOL,
                bar_interval="1d",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 31),
            )
            assert stored == []
