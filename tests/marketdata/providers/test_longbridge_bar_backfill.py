"""Unit tests against a fake history-candlestick client - never touches the
real longport SDK, real credentials, or a network call. Mirrors
test_longbridge_history.py's shape.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from apps.api.app.core.config import Settings
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.providers.longbridge import (
    LongbridgeBarBackfillProvider,
    build_longbridge_bar_backfill_provider,
)


class FakeHistoryCandle:
    def __init__(self, *, open, high, low, close, volume, timestamp):
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.timestamp = timestamp


class FakeHistoryCandlestickClient:
    def __init__(self, *, results=None, error: Exception | None = None):
        self._results = results
        self._error = error
        self.calls: list[tuple] = []

    async def history_candlesticks_by_date(self, symbol, period, adjust_type, start, end):
        self.calls.append((symbol, period, adjust_type, start, end))
        if self._error is not None:
            raise self._error
        return self._results


def make_settings(**overrides) -> Settings:
    defaults = dict(_env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256")
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_returns_bars_oldest_first_regardless_of_sdk_order():
    # Deliberately out of order - the provider must sort by timestamp
    # itself, never trust the SDK's return order.
    candles = [
        FakeHistoryCandle(
            open="101", high="103", low="100", close="102", volume=1_000,
            timestamp=datetime(2026, 8, 25, tzinfo=UTC),
        ),
        FakeHistoryCandle(
            open="99", high="101", low="98", close="100", volume=900,
            timestamp=datetime(2026, 8, 23, tzinfo=UTC),
        ),
    ]
    client = FakeHistoryCandlestickClient(results=candles)
    provider = LongbridgeBarBackfillProvider(client)

    bars = await provider.get_bars(
        "AAPL.US", bar_interval="1d", start_date=date(2026, 8, 23), end_date=date(2026, 8, 25)
    )

    assert [bar.close for bar in bars] == [Decimal("100"), Decimal("102")]
    assert bars[0].symbol == "AAPL.US"
    assert bars[0].bar_interval == "1d"
    assert bars[0].source == "longbridge"
    assert bars[0].open == Decimal("99")
    assert bars[0].high == Decimal("101")
    assert bars[0].low == Decimal("98")
    assert bars[0].volume == 900


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bar_interval", "expected_period"),
    [
        ("1m", "Min_1"),
        ("5m", "Min_5"),
        ("15m", "Min_15"),
        ("30m", "Min_30"),
        ("1h", "Min_60"),
        ("1d", "Day"),
    ],
)
async def test_each_interval_asks_the_vendor_for_the_matching_period(
    bar_interval: str, expected_period: str
):
    """Phase 70 (D088). This provider accepted only `1d` until this phase.

    **What this proves and what it does not.** It proves the adapter asks
    the SDK for the right `Period` member for each interval in the
    `BarInterval` vocabulary - which is the part of the mapping this
    codebase owns, and the part where a mistake would be invisible
    (persisting daily bars under a `bar_interval` of `"5m"`). It does NOT
    prove Longbridge returns intraday candles for any given symbol or
    entitlement: that needs a live call with a valid token, and the token
    available when this was written was expired. See D088 section 4.
    """
    candle = FakeHistoryCandle(
        open="99", high="101", low="98", close="100", volume=900,
        timestamp=datetime(2026, 8, 23, 14, 30, tzinfo=UTC),
    )
    client = FakeHistoryCandlestickClient(results=[candle])
    provider = LongbridgeBarBackfillProvider(client)

    bars = await provider.get_bars(
        "AAPL.US",
        bar_interval=bar_interval,
        start_date=date(2026, 8, 23),
        end_date=date(2026, 8, 25),
    )

    # The period the SDK was actually handed, compared by NAME rather than
    # by importing `Period` here - the vendor package stays out of this
    # test file exactly as it stays out of the provider's module scope.
    (_symbol, period, _adjust, _start, _end) = client.calls[0]
    assert getattr(period, "name", str(period)).endswith(expected_period)

    # And the bar is tagged with OUR interval, not the vendor's spelling -
    # this is the string every later reader matches on exactly.
    assert bars[0].bar_interval == bar_interval


@pytest.mark.asyncio
async def test_an_unmappable_interval_raises_rather_than_falling_back_to_daily():
    """The failure mode this refusal exists to prevent is silent and
    undetectable: falling back to `Period.Day` would persist DAILY bars
    under whatever `bar_interval` was requested, and every later reader -
    backtest, deployment, chart - would treat them as that interval's data.
    Nothing downstream could tell."""
    provider = LongbridgeBarBackfillProvider(FakeHistoryCandlestickClient(results=[]))

    with pytest.raises(ValueError, match="cannot map bar_interval"):
        await provider.get_bars(
            "AAPL.US", bar_interval="1w", start_date=date(2026, 8, 1), end_date=date(2026, 8, 2)
        )


@pytest.mark.asyncio
async def test_empty_results_is_data_unavailable():
    client = FakeHistoryCandlestickClient(results=[])
    provider = LongbridgeBarBackfillProvider(client)

    with pytest.raises(DataUnavailableError):
        await provider.get_bars(
            "NOSUCH.US", bar_interval="1d", start_date=date(2026, 8, 1), end_date=date(2026, 8, 2)
        )


@pytest.mark.asyncio
async def test_an_sdk_exception_becomes_a_typed_vendor_error():
    client = FakeHistoryCandlestickClient(error=RuntimeError("rate limited"))
    provider = LongbridgeBarBackfillProvider(client)

    with pytest.raises(VendorError, match="rate limited"):
        await provider.get_bars(
            "AAPL.US", bar_interval="1d", start_date=date(2026, 8, 1), end_date=date(2026, 8, 2)
        )


def test_build_returns_none_when_no_credentials_are_configured():
    settings = make_settings()
    assert build_longbridge_bar_backfill_provider(settings) is None


def test_build_returns_none_when_only_some_credentials_are_configured():
    settings = make_settings(longport_app_key="key", longport_app_secret="secret")
    assert build_longbridge_bar_backfill_provider(settings) is None
