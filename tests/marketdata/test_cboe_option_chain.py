"""Cboe delayed option chains and the Longbridge fallback (Phase 98, D117)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.api.app.marketdata.option_chain_provider import OptionChain
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.providers import cboe
from apps.api.app.marketdata.providers.cboe import (
    CboeDelayedOptionChainProvider,
    FallbackOptionChainProvider,
    parse_occ,
)
from apps.api.app.options.pricing import OptionRight

TODAY = date(2026, 9, 26)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(cboe, "today_in_new_york", lambda: TODAY)


def _doc(rows):
    return {"data": {"last_trade_time": "2026-09-25T15:59:59", "options": rows}}


ROWS = [
    {"option": "TQQQ260923C00080000", "bid": 1, "ask": 1.1},  # already expired
    {
        "option": "TQQQ260930C00080000",
        "bid": 1.46,
        "ask": 1.54,
        "iv": 0.4524,
        "delta": 0.4701,
        "open_interest": 2886.0,
        "volume": 0.0,
        "last_trade_price": 1.5,
    },
    {"option": "TQQQ260930P00080000", "bid": 0, "ask": 0.05},
    {"option": "TQQQ261002C00081500", "bid": 2.0, "ask": 2.2},
]


def test_occ_symbols_parse_to_expiry_right_and_strike():
    assert parse_occ("TQQQ261002C00081500") == (
        date(2026, 10, 2),
        OptionRight.CALL,
        Decimal("81.5"),
    )
    assert parse_occ("garbage") is None


@pytest.mark.asyncio
async def test_expired_contracts_are_not_offered_as_expiries():
    p = CboeDelayedOptionChainProvider(fetch=lambda root: _doc(ROWS))
    assert await p.get_expiries("TQQQ.US") == [date(2026, 9, 30), date(2026, 10, 2)]


@pytest.mark.asyncio
async def test_a_chain_keeps_the_vendors_fields_and_labels_itself_delayed():
    p = CboeDelayedOptionChainProvider(fetch=lambda root: _doc(ROWS))
    chain = await p.get_chain("TQQQ.US", date(2026, 9, 30))
    assert chain.source == "cboe-delayed"
    call = next(q for q in chain.quotes if q.right is OptionRight.CALL)
    assert call.bid == Decimal("1.46") and call.delta == Decimal("0.4701")
    assert call.open_interest == 2886 and call.volume == 0  # zero volume is a measurement
    put = next(q for q in chain.quotes if q.right is OptionRight.PUT)
    assert put.bid == Decimal("0") and put.implied_vol is None  # absent stays None


@pytest.mark.asyncio
async def test_one_download_serves_every_expiry():
    calls: list[str] = []

    def fetch(root):
        calls.append(root)
        return _doc(ROWS)

    p = CboeDelayedOptionChainProvider(fetch=fetch)
    await p.get_expiries("TQQQ.US")
    await p.get_chain("TQQQ.US", date(2026, 9, 30))
    await p.get_chain("TQQQ.US", date(2026, 10, 2))
    assert calls == ["TQQQ"]


@pytest.mark.asyncio
async def test_non_us_symbols_are_refused():
    p = CboeDelayedOptionChainProvider(fetch=lambda root: _doc(ROWS))
    with pytest.raises(DataUnavailableError):
        await p.get_expiries("700.HK")


class _Longbridge:
    name = "longbridge"

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.chain_calls = 0

    async def get_expiries(self, underlying):
        return [TODAY - timedelta(days=3), date(2026, 9, 30)]

    async def get_chain(self, underlying, expiry):
        self.chain_calls += 1
        if self.error:
            raise self.error
        return OptionChain(underlying, expiry, None, "longbridge", ())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_fallback_drops_past_expiries_from_the_primary():
    f = FallbackOptionChainProvider(
        _Longbridge(), CboeDelayedOptionChainProvider(fetch=lambda r: _doc(ROWS))
    )
    assert await f.get_expiries("TQQQ.US") == [date(2026, 9, 30)]


@pytest.mark.asyncio
async def test_an_entitlement_refusal_switches_to_cboe_and_stays_switched():
    primary = _Longbridge(VendorError("option quote failed: code=301604 no quote access"))
    f = FallbackOptionChainProvider(
        primary, CboeDelayedOptionChainProvider(fetch=lambda r: _doc(ROWS))
    )
    first = await f.get_chain("TQQQ.US", date(2026, 9, 30))
    second = await f.get_chain("TQQQ.US", date(2026, 10, 2))
    assert first.source == second.source == "cboe-delayed"
    assert primary.chain_calls == 1  # not asked again after the entitlement refusal


@pytest.mark.asyncio
async def test_any_other_primary_failure_is_not_hidden():
    f = FallbackOptionChainProvider(
        _Longbridge(VendorError("connection reset")),
        CboeDelayedOptionChainProvider(fetch=lambda r: _doc(ROWS)),
    )
    with pytest.raises(VendorError):
        await f.get_chain("TQQQ.US", date(2026, 9, 30))


@pytest.mark.asyncio
async def test_an_expired_expiry_is_refused_before_any_vendor_call():
    primary = _Longbridge()
    f = FallbackOptionChainProvider(
        primary, CboeDelayedOptionChainProvider(fetch=lambda r: _doc(ROWS))
    )
    with pytest.raises(DataUnavailableError):
        await f.get_chain("TQQQ.US", date(2026, 9, 23))
    assert primary.chain_calls == 0
