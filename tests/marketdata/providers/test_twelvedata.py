"""Twelve Data as the FX bar vendor (Phase 106, D132). No network: the
provider's fetch is replaced, so these pin parsing, paging and refusals."""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.providers import twelvedata as mod
from apps.api.app.marketdata.providers.twelvedata import (
    TwelveDataFxBarProvider,
    build_twelvedata_fx_bar_provider,
    twelvedata_symbol,
)


def test_symbols_map_to_the_vendor_spelling():
    assert twelvedata_symbol("EURUSD.FX") == "EUR/USD"
    assert twelvedata_symbol("xauusd.fx") == "XAU/USD"


@pytest.mark.parametrize(
    ("choice", "key", "built"),
    [("auto", "k", True), ("auto", None, False), ("twelvedata", "k", True),
     ("none", "k", False), ("ig", "k", False)],
)
def test_the_provider_is_built_only_when_chosen_and_keyed(choice, key, built):
    s = SimpleNamespace(fx_bar_provider=choice, twelvedata_api_key=key)
    assert (build_twelvedata_fx_bar_provider(s) is not None) is built


@pytest.mark.asyncio
async def test_rows_become_mid_bars_with_no_volume(monkeypatch):
    async def fake(self, vendor_symbol, interval, start, end):
        assert vendor_symbol == "EUR/USD" and interval == "5min"
        return [
            {"datetime": "2026-09-29 08:00:00", "open": "1.1", "high": "1.2", "low": "1.0",
             "close": "1.15"},
            {"datetime": "2026-09-29 08:05:00", "open": "1.15", "high": "1.2", "low": "1.1",
             "close": "not-a-number"},  # dropped, never guessed
        ]

    monkeypatch.setattr(TwelveDataFxBarProvider, "_fetch", fake)
    bars = await TwelveDataFxBarProvider("k").get_bars(
        "EURUSD.FX", bar_interval="5m", start_date=date(2026, 9, 29), end_date=date(2026, 9, 29)
    )
    assert len(bars) == 1
    b = bars[0]
    assert b.ts == datetime(2026, 9, 29, 8, 0, tzinfo=UTC)
    assert b.close == Decimal("1.15") and b.volume is None and b.source == "twelvedata"


@pytest.mark.asyncio
async def test_an_empty_window_is_data_unavailable(monkeypatch):
    async def fake(self, *a):
        return []

    monkeypatch.setattr(TwelveDataFxBarProvider, "_fetch", fake)
    with pytest.raises(DataUnavailableError):
        await TwelveDataFxBarProvider("k").get_bars(
            "EURUSD.FX", bar_interval="1h", start_date=date(2026, 9, 26),
            end_date=date(2026, 9, 27),
        )


@pytest.mark.asyncio
async def test_a_vendor_error_never_echoes_the_api_key(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.HTTPError(
            "https://api.twelvedata.com/time_series?apikey=SECRETKEY", 500, "x", {}, None
        )

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    with pytest.raises(VendorError) as err:
        await TwelveDataFxBarProvider("SECRETKEY").get_bars(
            "EURUSD.FX", bar_interval="5m", start_date=date(2026, 9, 29),
            end_date=date(2026, 9, 29),
        )
    assert "SECRETKEY" not in str(err.value)
