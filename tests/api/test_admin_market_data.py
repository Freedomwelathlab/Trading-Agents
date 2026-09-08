"""Integration tests for POST /admin/market-data/backfill (Phase 53,
docs/DECISIONS.md D070), against a real Postgres instance. Reuses the
existing admin/non-admin fixtures from test_admin.py rather than
duplicating them.
"""

import contextlib
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete

from apps.api.app.api.dependencies import get_market_data_bar_backfill_provider
from apps.api.app.db.models import MarketDataBackfillJob, MarketDataBar
from apps.api.app.main import app
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from tests.api.test_admin import _get_token, admin_user, api_client, db_session, non_admin_user

TEST_SYMBOL = "TESTADMINBF.US"


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

    async def get_bars(self, symbol, *, bar_interval, start_date, end_date):
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
async def test_non_admin_cannot_trigger_a_backfill():
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                "/admin/market-data/backfill",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "symbol": TEST_SYMBOL,
                    "start_date": "2026-08-18",
                    "end_date": "2026-08-20",
                },
            )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_no_provider_configured_is_not_configured_not_a_fabricated_success():
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with api_client() as client:
            app.dependency_overrides[get_market_data_bar_backfill_provider] = lambda: None
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    "/admin/market-data/backfill",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": TEST_SYMBOL,
                        "start_date": "2026-08-18",
                        "end_date": "2026-08-20",
                    },
                )
            finally:
                del app.dependency_overrides[get_market_data_bar_backfill_provider]
    assert response.status_code == 503
    assert "NOT_CONFIGURED" in response.text


@pytest.mark.asyncio
async def test_admin_can_trigger_a_successful_backfill():
    async with db_session() as session, admin_user(session) as (_uid, email), clean_up(session):
        fake_provider = FakeBarBackfillProvider(bars=[_bar(18, "100"), _bar(20, "102")])
        async with api_client() as client:
            app.dependency_overrides[get_market_data_bar_backfill_provider] = lambda: fake_provider
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    "/admin/market-data/backfill",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": TEST_SYMBOL.lower(),
                        "start_date": "2026-08-18",
                        "end_date": "2026-08-20",
                    },
                )
            finally:
                del app.dependency_overrides[get_market_data_bar_backfill_provider]

        assert response.status_code == 201, response.text
        body = response.json()
        # Normalized to upper-case, same convention as watchlist symbols.
        assert body["symbol"] == TEST_SYMBOL
        assert body["status"] == "succeeded"
        assert body["bars_ingested"] == 2
        assert body["earliest_bar_date"] == "2026-08-18"
        assert body["latest_bar_date"] == "2026-08-20"
        assert body["error_detail"] is None


@pytest.mark.asyncio
async def test_a_vendor_failure_returns_201_with_a_failed_status_not_a_5xx():
    async with db_session() as session, admin_user(session) as (_uid, email), clean_up(session):
        fake_provider = FakeBarBackfillProvider(error=VendorError("rate limited"))
        async with api_client() as client:
            app.dependency_overrides[get_market_data_bar_backfill_provider] = lambda: fake_provider
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    "/admin/market-data/backfill",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": TEST_SYMBOL,
                        "start_date": "2026-08-18",
                        "end_date": "2026-08-20",
                    },
                )
            finally:
                del app.dependency_overrides[get_market_data_bar_backfill_provider]

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "failed"
        assert body["bars_ingested"] == 0
        assert "rate limited" in body["error_detail"]


@pytest.mark.asyncio
async def test_a_data_unavailable_response_is_also_a_failed_job_not_a_5xx():
    async with db_session() as session, admin_user(session) as (_uid, email), clean_up(session):
        fake_provider = FakeBarBackfillProvider(
            error=DataUnavailableError(f"Longbridge returned no candlesticks for {TEST_SYMBOL!r}.")
        )
        async with api_client() as client:
            app.dependency_overrides[get_market_data_bar_backfill_provider] = lambda: fake_provider
            try:
                token = await _get_token(client, email)
                response = await client.post(
                    "/admin/market-data/backfill",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": TEST_SYMBOL,
                        "start_date": "2026-08-18",
                        "end_date": "2026-08-20",
                    },
                )
            finally:
                del app.dependency_overrides[get_market_data_bar_backfill_provider]

        assert response.status_code == 201, response.text
        assert response.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_bar_interval_other_than_1d_is_a_422():
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                "/admin/market-data/backfill",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "symbol": TEST_SYMBOL,
                    "bar_interval": "5m",
                    "start_date": "2026-08-18",
                    "end_date": "2026-08-20",
                },
            )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_end_date_before_start_date_is_a_422():
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                "/admin/market-data/backfill",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "symbol": TEST_SYMBOL,
                    "start_date": "2026-08-20",
                    "end_date": "2026-08-18",
                },
            )
    assert response.status_code == 422
