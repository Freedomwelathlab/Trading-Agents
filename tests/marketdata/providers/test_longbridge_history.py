"""Unit tests against a fake candlestick client - never touches the real
longport SDK, real credentials, or a network call. Mirrors
test_longbridge.py's shape.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.core.config import Settings
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.providers.longbridge import (
    LongbridgeHistoryProvider,
    build_longbridge_history_provider,
)


class FakeCandle:
    def __init__(self, *, close, timestamp):
        self.close = close
        self.timestamp = timestamp


class FakeCandlestickClient:
    def __init__(self, *, results=None, error: Exception | None = None):
        self._results = results
        self._error = error
        self.calls: list[tuple] = []

    async def candlesticks(self, symbol, period, count, adjust_type):
        self.calls.append((symbol, period, count, adjust_type))
        if self._error is not None:
            raise self._error
        return self._results


def make_settings(**overrides) -> Settings:
    defaults = dict(_env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256")
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_returns_closes_oldest_first_regardless_of_sdk_order():
    # Deliberately out of order - the provider must sort by timestamp
    # itself, never trust the SDK's return order.
    candles = [
        FakeCandle(close="102", timestamp=datetime(2026, 8, 25, tzinfo=UTC)),
        FakeCandle(close="100", timestamp=datetime(2026, 8, 23, tzinfo=UTC)),
        FakeCandle(close="101", timestamp=datetime(2026, 8, 24, tzinfo=UTC)),
    ]
    client = FakeCandlestickClient(results=candles)
    provider = LongbridgeHistoryProvider(client)

    closes = await provider.get_daily_closes("AAPL.US", count=3)

    assert closes == [Decimal("100"), Decimal("101"), Decimal("102")]
    assert client.calls[0][0] == "AAPL.US"
    assert client.calls[0][2] == 3


@pytest.mark.asyncio
async def test_empty_results_is_data_unavailable():
    client = FakeCandlestickClient(results=[])
    provider = LongbridgeHistoryProvider(client)

    with pytest.raises(DataUnavailableError):
        await provider.get_daily_closes("NOSUCH.US", count=20)


@pytest.mark.asyncio
async def test_an_sdk_exception_becomes_a_typed_vendor_error():
    client = FakeCandlestickClient(error=RuntimeError("rate limited"))
    provider = LongbridgeHistoryProvider(client)

    with pytest.raises(VendorError, match="rate limited"):
        await provider.get_daily_closes("AAPL.US", count=20)


def test_build_returns_none_when_no_credentials_are_configured():
    settings = make_settings()
    assert build_longbridge_history_provider(settings) is None


def test_build_returns_none_when_only_some_credentials_are_configured():
    settings = make_settings(longport_app_key="key", longport_app_secret="secret")
    assert build_longbridge_history_provider(settings) is None
