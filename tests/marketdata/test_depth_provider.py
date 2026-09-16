"""Depth-adapter tests (Phase 74, D092).

The whole substance of this adapter is one filter, so that is what these
pin: a vendor level without a real price is not a level. The vendor
answers 200 with a structurally valid book whose prices are null when it
has nothing, and passing those through as zeros would draw a tight,
empty, entirely fictional order book.
"""

from decimal import Decimal

import pytest

from apps.api.app.marketdata.depth_provider import DepthLevel, OrderBook
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.providers.longbridge import LongbridgeDepthProvider


class FakeLevel:
    def __init__(self, price, volume, order_num=None):
        self.price = price
        self.volume = volume
        self.order_num = order_num


class FakeBook:
    def __init__(self, bids=(), asks=()):
        self.bids = list(bids)
        self.asks = list(asks)


class FakeDepthClient:
    def __init__(self, book=None, error: Exception | None = None):
        self._book = book
        self._error = error
        self.calls: list[str] = []

    async def depth(self, symbol):
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return self._book


@pytest.mark.asyncio
async def test_a_book_whose_levels_have_no_price_is_data_unavailable():
    """The exact shape the vendor returns for a US symbol on this account.

    Measured live: `depth("TQQQ.US")` answers with one bid and one ask
    whose price is None and whose volume is 0, while `700.HK` returns
    genuine levels. Two explanations fit - an LV1 entitlement carrying no
    ladder, or simply a closed market - and one observation cannot
    separate them. It does not matter here: an absent price is not a
    level under either.
    """
    client = FakeDepthClient(
        FakeBook(bids=[FakeLevel(None, 0)], asks=[FakeLevel(None, 0)])
    )
    provider = LongbridgeDepthProvider(client)

    with pytest.raises(DataUnavailableError, match="no priced order-book level"):
        await provider.get_depth("TQQQ.US")


@pytest.mark.asyncio
async def test_a_zero_priced_level_is_dropped_not_reported_as_a_bid_at_zero():
    """A zero bid is a price. A spread measured against it is a number a
    human or a strategy would act on, and it would be fiction."""
    client = FakeDepthClient(
        FakeBook(bids=[FakeLevel("0", 500)], asks=[FakeLevel("0", 500)])
    )
    provider = LongbridgeDepthProvider(client)

    with pytest.raises(DataUnavailableError):
        await provider.get_depth("TQQQ.US")


@pytest.mark.asyncio
async def test_a_real_book_is_returned_best_price_first():
    client = FakeDepthClient(
        FakeBook(
            bids=[FakeLevel("434.20", 5700, 12), FakeLevel("434.10", 3300, 8)],
            asks=[FakeLevel("434.40", 7800, 15)],
        )
    )
    provider = LongbridgeDepthProvider(client)

    book = await provider.get_depth("700.HK")

    assert book.symbol == "700.HK"
    assert book.source == "longbridge"
    assert book.best_bid == DepthLevel(Decimal("434.20"), 5700, 12)
    assert book.best_ask == DepthLevel(Decimal("434.40"), 7800, 15)
    assert book.spread == Decimal("0.20")


@pytest.mark.asyncio
async def test_unpriced_levels_are_dropped_while_priced_ones_survive():
    """A partially populated book is real. Dropping the whole response
    because one level was empty would discard genuine liquidity."""
    client = FakeDepthClient(
        FakeBook(
            bids=[FakeLevel("10.00", 100), FakeLevel(None, 0), FakeLevel("9.99", 50)],
            asks=[FakeLevel(None, 0)],
        )
    )
    provider = LongbridgeDepthProvider(client)

    book = await provider.get_depth("X.US")

    assert [lvl.price for lvl in book.bids] == [Decimal("10.00"), Decimal("9.99")]
    assert book.asks == ()


@pytest.mark.asyncio
async def test_a_one_sided_book_has_no_spread():
    """`None`, not zero and not a number derived from the last trade. A
    one-sided book genuinely has no spread."""
    client = FakeDepthClient(FakeBook(bids=[FakeLevel("10.00", 100)], asks=[]))
    provider = LongbridgeDepthProvider(client)

    book = await provider.get_depth("X.US")

    assert book.best_bid is not None
    assert book.best_ask is None
    assert book.spread is None
    assert book.is_empty is False


@pytest.mark.asyncio
async def test_a_vendor_exception_becomes_a_typed_vendor_error():
    provider = LongbridgeDepthProvider(FakeDepthClient(error=RuntimeError("rate limited")))

    with pytest.raises(VendorError, match="rate limited"):
        await provider.get_depth("X.US")


def test_an_empty_book_reports_itself_as_empty():
    from datetime import UTC, datetime

    book = OrderBook(
        symbol="X", bids=(), asks=(), as_of=datetime(2026, 9, 16, tzinfo=UTC), source="t"
    )
    assert book.is_empty is True
    assert book.spread is None
