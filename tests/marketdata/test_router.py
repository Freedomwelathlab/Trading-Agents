from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.marketdata.models import MarketSnapshot
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.router import MarketDataRouter, NoDataAvailableError

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


class FakeProvider:
    """Test double only - never shipped as a real vendor. Configured with
    either a snapshot to return or an exception to raise."""

    def __init__(
        self,
        name: str,
        *,
        snapshot: MarketSnapshot | None = None,
        error: Exception | None = None,
    ):
        self.name = name
        self._snapshot = snapshot
        self._error = error
        self.calls: list[str] = []

    async def get_snapshot(self, symbol: str) -> MarketSnapshot:
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        assert self._snapshot is not None
        return self._snapshot


def make_snapshot(**overrides) -> MarketSnapshot:
    defaults = dict(symbol="AAPL", price=Decimal(100), as_of=NOW, source="fake")
    defaults.update(overrides)
    return MarketSnapshot(**defaults)


def test_empty_provider_list_is_rejected_at_construction():
    with pytest.raises(ValueError, match="at least one provider"):
        MarketDataRouter([])


async def test_returns_the_first_provider_that_succeeds():
    good = FakeProvider("good", snapshot=make_snapshot())
    router = MarketDataRouter([good])
    snapshot = await router.get_snapshot("AAPL")
    assert snapshot.source == "fake"
    assert good.calls == ["AAPL"]


async def test_falls_through_to_the_next_provider_on_data_unavailable():
    first = FakeProvider("first", error=DataUnavailableError("delisted"))
    second = FakeProvider("second", snapshot=make_snapshot(source="second"))
    router = MarketDataRouter([first, second])

    snapshot = await router.get_snapshot("AAPL")

    assert snapshot.source == "second"
    assert first.calls == ["AAPL"]
    assert second.calls == ["AAPL"]


async def test_falls_through_to_the_next_provider_on_vendor_error():
    first = FakeProvider("first", error=VendorError("rate limited"))
    second = FakeProvider("second", snapshot=make_snapshot(source="second"))
    router = MarketDataRouter([first, second])

    snapshot = await router.get_snapshot("AAPL")

    assert snapshot.source == "second"


async def test_raises_no_data_available_with_sentinel_prefix_when_every_provider_fails():
    first = FakeProvider("first", error=VendorError("down"))
    second = FakeProvider("second", error=DataUnavailableError("no such symbol"))
    router = MarketDataRouter([first, second])

    with pytest.raises(NoDataAvailableError, match=r"^NO_DATA_AVAILABLE:") as exc_info:
        await router.get_snapshot("BOGUS")

    assert "first: down" in str(exc_info.value)
    assert "second: no such symbol" in str(exc_info.value)


async def test_never_falls_through_on_an_unrelated_exception():
    """A bug in a provider (e.g. a KeyError) must not be silently
    swallowed and treated the same as 'no data' - only the two typed
    exceptions trigger fallback."""
    broken = FakeProvider("broken", error=RuntimeError("unexpected bug"))
    never_called = FakeProvider("never_called", snapshot=make_snapshot())
    router = MarketDataRouter([broken, never_called])

    with pytest.raises(RuntimeError, match="unexpected bug"):
        await router.get_snapshot("AAPL")

    assert never_called.calls == []
