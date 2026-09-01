"""Unit tests against a fake news client - never touches the real longport
SDK, real credentials, or a network call (D059). The fake mimics the SDK's
real `NewsItem` shape as verified by introspection of longport 4.3.7:
`.title`, `.published_at`, `.url`.
"""

from datetime import UTC, datetime

import pytest

from apps.api.app.core.config import Settings
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.providers.longbridge import (
    LongbridgeNewsProvider,
    build_longbridge_news_provider,
)


class FakeNewsItem:
    def __init__(self, *, title, published_at, url=""):
        self.title = title
        self.published_at = published_at
        self.url = url


class FakeNewsClient:
    def __init__(self, *, items=None, error: Exception | None = None):
        self._items = items
        self._error = error
        self.calls: list[str] = []

    async def news(self, symbol):
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._items


def make_settings(**overrides) -> Settings:
    defaults = dict(_env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256")
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_headlines_come_back_newest_first_regardless_of_sdk_order():
    items = [
        FakeNewsItem(title="middle", published_at=datetime(2026, 8, 20, tzinfo=UTC)),
        FakeNewsItem(title="oldest", published_at=datetime(2026, 8, 18, tzinfo=UTC)),
        FakeNewsItem(
            title="newest", published_at=datetime(2026, 8, 25, tzinfo=UTC), url="https://x/1"
        ),
    ]
    provider = LongbridgeNewsProvider(FakeNewsClient(items=items))

    headlines = await provider.get_recent_headlines("AAPL.US", limit=10)

    assert [h.title for h in headlines] == ["newest", "middle", "oldest"]
    assert headlines[0].url == "https://x/1"
    assert headlines[1].url is None


@pytest.mark.asyncio
async def test_limit_keeps_the_newest_items_and_never_pads():
    items = [
        FakeNewsItem(title=f"item-{day}", published_at=datetime(2026, 8, day, tzinfo=UTC))
        for day in range(1, 6)
    ]
    provider = LongbridgeNewsProvider(FakeNewsClient(items=items))

    headlines = await provider.get_recent_headlines("AAPL.US", limit=2)

    assert [h.title for h in headlines] == ["item-5", "item-4"]

    # Fewer real items than the limit returns fewer - never padded.
    short = LongbridgeNewsProvider(FakeNewsClient(items=items[:1]))
    assert len(await short.get_recent_headlines("AAPL.US", limit=10)) == 1


@pytest.mark.asyncio
async def test_epoch_second_timestamps_are_normalized_to_utc():
    epoch = int(datetime(2026, 8, 20, 12, tzinfo=UTC).timestamp())
    provider = LongbridgeNewsProvider(
        FakeNewsClient(items=[FakeNewsItem(title="epoch item", published_at=epoch)])
    )

    headlines = await provider.get_recent_headlines("AAPL.US", limit=5)

    assert headlines[0].published_at == datetime(2026, 8, 20, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_items_without_a_title_or_a_real_date_are_skipped_never_synthesized():
    items = [
        FakeNewsItem(title="", published_at=datetime(2026, 8, 25, tzinfo=UTC)),
        FakeNewsItem(title="no date", published_at=None),
        FakeNewsItem(title="good", published_at=datetime(2026, 8, 24, tzinfo=UTC)),
    ]
    provider = LongbridgeNewsProvider(FakeNewsClient(items=items))

    headlines = await provider.get_recent_headlines("AAPL.US", limit=10)

    assert [h.title for h in headlines] == ["good"]


@pytest.mark.asyncio
async def test_no_usable_items_is_data_unavailable():
    provider = LongbridgeNewsProvider(FakeNewsClient(items=[]))

    with pytest.raises(DataUnavailableError):
        await provider.get_recent_headlines("NOSUCH.US", limit=10)


@pytest.mark.asyncio
async def test_only_unusable_items_is_also_data_unavailable():
    provider = LongbridgeNewsProvider(
        FakeNewsClient(items=[FakeNewsItem(title="", published_at=None)])
    )

    with pytest.raises(DataUnavailableError):
        await provider.get_recent_headlines("NOSUCH.US", limit=10)


@pytest.mark.asyncio
async def test_an_sdk_exception_becomes_a_typed_vendor_error():
    provider = LongbridgeNewsProvider(FakeNewsClient(error=RuntimeError("rate limited")))

    with pytest.raises(VendorError, match="rate limited"):
        await provider.get_recent_headlines("AAPL.US", limit=10)


@pytest.mark.asyncio
async def test_a_non_positive_limit_is_rejected():
    provider = LongbridgeNewsProvider(FakeNewsClient(items=[]))

    with pytest.raises(ValueError, match="at least 1"):
        await provider.get_recent_headlines("AAPL.US", limit=0)


def test_build_returns_none_when_no_credentials_are_configured():
    assert build_longbridge_news_provider(make_settings()) is None


def test_build_returns_none_when_only_some_credentials_are_configured():
    settings = make_settings(longport_app_key="key", longport_access_token="token")
    assert build_longbridge_news_provider(settings) is None
