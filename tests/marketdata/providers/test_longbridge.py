"""Unit tests against a fake LongbridgeQuoteClient - never touches the
real longport SDK, real credentials, or a network call. The provider's
dependency on the SDK is confined to build_longbridge_provider(), which
these tests deliberately don't exercise past its "not configured" branch
(constructing a real AsyncQuoteContext with fake credentials could still
attempt a real connection, which has no place in a unit test).
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.core.config import Settings
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.providers.longbridge import (
    LongbridgeMarketDataProvider,
    build_longbridge_provider,
)


class FakeSecurityQuote:
    def __init__(self, *, last_done, timestamp):
        self.last_done = last_done
        self.timestamp = timestamp


class FakeQuoteClient:
    def __init__(self, *, results=None, error: Exception | None = None):
        self._results = results
        self._error = error
        self.calls: list[list[str]] = []

    async def quote(self, symbols: list[str]):
        self.calls.append(symbols)
        if self._error is not None:
            raise self._error
        return self._results


def make_settings(**overrides) -> Settings:
    defaults = dict(_env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256")
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_returns_a_snapshot_built_from_the_quote():
    quote = FakeSecurityQuote(last_done="123.45", timestamp=datetime(2026, 8, 23, 12, tzinfo=UTC))
    client = FakeQuoteClient(results=[quote])
    provider = LongbridgeMarketDataProvider(client)

    snapshot = await provider.get_snapshot("AAPL.US")

    assert snapshot.symbol == "AAPL.US"
    assert snapshot.price == Decimal("123.45")
    assert snapshot.source == "longbridge"
    assert client.calls == [["AAPL.US"]]


@pytest.mark.asyncio
async def test_naive_timestamp_from_the_sdk_is_treated_as_utc():
    client = FakeQuoteClient(
        results=[FakeSecurityQuote(last_done="100", timestamp=datetime(2026, 8, 23, 12, 0, 0))]
    )
    provider = LongbridgeMarketDataProvider(client)

    snapshot = await provider.get_snapshot("700.HK")

    assert snapshot.as_of.tzinfo is not None


@pytest.mark.asyncio
async def test_epoch_int_timestamp_from_the_sdk_is_converted():
    epoch = int(datetime(2026, 8, 23, 12, tzinfo=UTC).timestamp())
    client = FakeQuoteClient(results=[FakeSecurityQuote(last_done="100", timestamp=epoch)])
    provider = LongbridgeMarketDataProvider(client)

    snapshot = await provider.get_snapshot("700.HK")

    assert snapshot.as_of == datetime(2026, 8, 23, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_empty_results_is_data_unavailable():
    client = FakeQuoteClient(results=[])
    provider = LongbridgeMarketDataProvider(client)

    with pytest.raises(DataUnavailableError):
        await provider.get_snapshot("NOSUCH.US")


@pytest.mark.asyncio
async def test_non_positive_price_is_data_unavailable():
    client = FakeQuoteClient(
        results=[FakeSecurityQuote(last_done="0", timestamp=datetime(2026, 8, 23, tzinfo=UTC))]
    )
    provider = LongbridgeMarketDataProvider(client)

    with pytest.raises(DataUnavailableError):
        await provider.get_snapshot("AAPL.US")


@pytest.mark.asyncio
async def test_an_sdk_exception_becomes_a_typed_vendor_error():
    client = FakeQuoteClient(error=RuntimeError("rate limited"))
    provider = LongbridgeMarketDataProvider(client)

    with pytest.raises(VendorError, match="rate limited"):
        await provider.get_snapshot("AAPL.US")


def test_build_returns_none_when_no_credentials_are_configured():
    settings = make_settings()
    assert build_longbridge_provider(settings) is None


def test_build_returns_none_when_only_some_credentials_are_configured():
    settings = make_settings(longport_app_key="key", longport_app_secret="secret")
    # access token missing - partial config is treated as no config, not guessed at
    assert build_longbridge_provider(settings) is None
