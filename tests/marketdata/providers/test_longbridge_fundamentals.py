"""Unit tests against a fake fundamentals client - never touches the real
longport SDK, real credentials, or a network call (D059). Mirrors
test_longbridge_history.py's shape.

The fake objects deliberately mimic the SDK's real response shape as
verified by introspection of longport 4.3.7: `ValuationData.metrics` is a
`ValuationMetricsData` whose `pe`/`pb`/`ps`/`dvd_yld` are each a
`ValuationMetricData | None` carrying `.list[ValuationPoint]`, and a
`ValuationPoint` has `.timestamp` and a string `.value`.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.core.config import Settings
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.providers.longbridge import (
    LongbridgeFundamentalsProvider,
    build_longbridge_fundamentals_provider,
)


class FakePoint:
    def __init__(self, timestamp, value):
        self.timestamp = timestamp
        self.value = value


class FakeMetric:
    def __init__(self, points):
        self.list = points


class FakeMetrics:
    def __init__(self, *, pe=None, pb=None, ps=None, dvd_yld=None):
        self.pe = pe
        self.pb = pb
        self.ps = ps
        self.dvd_yld = dvd_yld


class FakeValuation:
    def __init__(self, metrics):
        self.metrics = metrics


class FakeOverview:
    def __init__(self, *, company_name="", name="", category=""):
        self.company_name = company_name
        self.name = name
        self.category = category


class FakeFundamentalsClient:
    def __init__(
        self,
        *,
        overview=None,
        valuation=None,
        company_error: Exception | None = None,
        valuation_error: Exception | None = None,
    ):
        self._overview = overview
        self._valuation = valuation
        self._company_error = company_error
        self._valuation_error = valuation_error
        self.calls: list[tuple[str, str]] = []

    async def company(self, symbol):
        self.calls.append(("company", symbol))
        if self._company_error is not None:
            raise self._company_error
        return self._overview

    async def valuation(self, symbol):
        self.calls.append(("valuation", symbol))
        if self._valuation_error is not None:
            raise self._valuation_error
        return self._valuation


def make_settings(**overrides) -> Settings:
    defaults = dict(_env_file=None, jwt_secret_key="test-secret-at-least-32-bytes-long-for-hs256")
    defaults.update(overrides)
    return Settings(**defaults)


def _full_valuation() -> FakeValuation:
    return FakeValuation(
        FakeMetrics(
            # Deliberately out of chronological order - the provider must
            # pick the newest by timestamp, never by list position.
            pe=FakeMetric(
                [
                    FakePoint(datetime(2026, 8, 1, tzinfo=UTC), "24.5"),
                    FakePoint(datetime(2026, 8, 20, tzinfo=UTC), "26.75"),
                    FakePoint(datetime(2026, 7, 1, tzinfo=UTC), "22.0"),
                ]
            ),
            pb=FakeMetric([FakePoint(datetime(2026, 8, 20, tzinfo=UTC), "8.1")]),
            ps=FakeMetric([FakePoint(datetime(2026, 8, 19, tzinfo=UTC), "6.25")]),
            dvd_yld=FakeMetric([FakePoint(datetime(2026, 8, 18, tzinfo=UTC), "0.45")]),
        )
    )


@pytest.mark.asyncio
async def test_real_vendor_fields_are_carried_through_verbatim():
    client = FakeFundamentalsClient(
        overview=FakeOverview(company_name="Apple Inc.", category="Technology"),
        valuation=_full_valuation(),
    )
    provider = LongbridgeFundamentalsProvider(client)

    fundamentals = await provider.get_fundamentals("AAPL.US")

    assert fundamentals.symbol == "AAPL.US"
    assert fundamentals.source == "longbridge"
    assert fundamentals.company_name == "Apple Inc."
    assert fundamentals.category == "Technology"
    # Newest point, not the first or the last in the list.
    assert fundamentals.pe == Decimal("26.75")
    assert fundamentals.pb == Decimal("8.1")
    assert fundamentals.ps == Decimal("6.25")
    assert fundamentals.dividend_yield == Decimal("0.45")
    assert fundamentals.as_of == datetime(2026, 8, 20, tzinfo=UTC)
    assert client.calls == [("company", "AAPL.US"), ("valuation", "AAPL.US")]


@pytest.mark.asyncio
async def test_a_missing_metric_stays_none_rather_than_becoming_zero():
    client = FakeFundamentalsClient(
        overview=FakeOverview(company_name="Loss Co"),
        valuation=FakeValuation(
            FakeMetrics(pe=None, pb=FakeMetric([FakePoint(datetime(2026, 8, 1, tzinfo=UTC), "2")]))
        ),
    )
    provider = LongbridgeFundamentalsProvider(client)

    fundamentals = await provider.get_fundamentals("LOSS.US")

    assert fundamentals.pe is None
    assert fundamentals.ps is None
    assert fundamentals.dividend_yield is None
    assert fundamentals.pb == Decimal("2")


@pytest.mark.asyncio
async def test_unparseable_metric_values_are_treated_as_missing_not_zero():
    client = FakeFundamentalsClient(
        overview=FakeOverview(company_name="Odd Co"),
        valuation=FakeValuation(
            FakeMetrics(
                pe=FakeMetric([FakePoint(datetime(2026, 8, 1, tzinfo=UTC), "")]),
                pb=FakeMetric([FakePoint(datetime(2026, 8, 1, tzinfo=UTC), "--")]),
            )
        ),
    )
    provider = LongbridgeFundamentalsProvider(client)

    fundamentals = await provider.get_fundamentals("ODD.US")

    assert fundamentals.pe is None
    assert fundamentals.pb is None
    # No usable dated metric at all -> as_of stays None, never "now".
    assert fundamentals.as_of is None


@pytest.mark.asyncio
async def test_epoch_second_timestamps_are_normalized_to_utc():
    epoch = int(datetime(2026, 8, 20, tzinfo=UTC).timestamp())
    client = FakeFundamentalsClient(
        overview=FakeOverview(name="Epoch Co"),
        valuation=FakeValuation(FakeMetrics(pe=FakeMetric([FakePoint(epoch, "10")]))),
    )
    provider = LongbridgeFundamentalsProvider(client)

    fundamentals = await provider.get_fundamentals("EPOCH.US")

    assert fundamentals.as_of == datetime(2026, 8, 20, tzinfo=UTC)
    # `name` is used only when `company_name` is empty.
    assert fundamentals.company_name == "Epoch Co"


@pytest.mark.asyncio
async def test_an_all_empty_response_is_data_unavailable():
    client = FakeFundamentalsClient(
        overview=FakeOverview(), valuation=FakeValuation(FakeMetrics())
    )
    provider = LongbridgeFundamentalsProvider(client)

    with pytest.raises(DataUnavailableError):
        await provider.get_fundamentals("NOSUCH.US")


@pytest.mark.asyncio
async def test_a_company_sdk_exception_becomes_a_typed_vendor_error():
    client = FakeFundamentalsClient(company_error=RuntimeError("rate limited"))
    provider = LongbridgeFundamentalsProvider(client)

    with pytest.raises(VendorError, match="rate limited"):
        await provider.get_fundamentals("AAPL.US")


@pytest.mark.asyncio
async def test_a_valuation_sdk_exception_becomes_a_typed_vendor_error():
    client = FakeFundamentalsClient(
        overview=FakeOverview(company_name="Apple Inc."),
        valuation_error=RuntimeError("auth failed"),
    )
    provider = LongbridgeFundamentalsProvider(client)

    with pytest.raises(VendorError, match="auth failed"):
        await provider.get_fundamentals("AAPL.US")


def test_build_returns_none_when_no_credentials_are_configured():
    assert build_longbridge_fundamentals_provider(make_settings()) is None


def test_build_returns_none_when_only_some_credentials_are_configured():
    settings = make_settings(longport_app_key="key", longport_app_secret="secret")
    assert build_longbridge_fundamentals_provider(settings) is None
