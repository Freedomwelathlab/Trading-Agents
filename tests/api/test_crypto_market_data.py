"""Crypto quote, order book and chart bars (Phase 106, D129).

BTC-USD on the Markets page had no chart and an order-book error: depth
and quotes went to Longbridge, which has no spot crypto (301600), and no
crypto bars were ever stored. Crypto now routes to Coinbase's public API by
symbol shape, and an empty or stale crypto chart is filled on read.
Equities keep the read-only bar contract (tests/api/test_market_data_bars.py).
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete

from apps.api.app.api import routes
from apps.api.app.api.dependencies import get_market_data_bar_backfill_provider
from apps.api.app.db.models import MarketDataBar
from apps.api.app.main import app
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.bar_router import BarBackfillRouter
from apps.api.app.marketdata.depth_provider import DepthLevel, OrderBook, RoutedDepthProvider
from apps.api.app.marketdata.models import MarketSnapshot
from apps.api.app.marketdata.provider import VendorError
from apps.api.app.marketdata.providers.coinbase_live import is_crypto_symbol, product_id
from apps.api.app.marketdata.router import MarketDataRouter
from tests.api.test_admin import _get_token, api_client, db_session, non_admin_user

SYMBOL = "TESTCOIN-USD"


@pytest.mark.parametrize(
    ("symbol", "crypto"),
    [("BTC-USD", True), ("btc/usd", True), ("SOL/USDT", True), ("TQQQ.US", False),
     ("BRK-B.US", False), ("EURUSD.FX", False), ("AAPL", False)],
)
def test_crypto_symbol_shapes(symbol, crypto):
    assert is_crypto_symbol(symbol) is crypto


def test_a_slash_symbol_maps_to_the_coinbase_product_id():
    assert product_id("btc/usd") == "BTC-USD"


class _Named:
    def __init__(self, name, crypto_only=False):
        self.name, self.asked = name, []
        if crypto_only:
            self.supports = is_crypto_symbol

    async def get_snapshot(self, symbol):
        self.asked.append(symbol)
        return MarketSnapshot(symbol=symbol, price=Decimal("1"), as_of=datetime.now(UTC),
                              source=self.name)

    async def get_depth(self, symbol):
        self.asked.append(symbol)
        lvl = (DepthLevel(price=Decimal("1"), volume=Decimal("0.5")),)
        return OrderBook(symbol=symbol, bids=lvl, asks=lvl, as_of=datetime.now(UTC),
                         source=self.name)


@pytest.mark.asyncio
async def test_quotes_never_ask_the_other_venue():
    coin, lb = _Named("coinbase", crypto_only=True), _Named("longbridge")
    router = MarketDataRouter([coin, lb])
    assert (await router.get_snapshot("BTC-USD")).source == "coinbase"
    assert (await router.get_snapshot("TQQQ.US")).source == "longbridge"
    assert coin.asked == ["BTC-USD"] and lb.asked == ["TQQQ.US"]


@pytest.mark.asyncio
async def test_depth_is_routed_by_symbol_shape():
    coin, lb = _Named("coinbase"), _Named("longbridge")
    routed = RoutedDepthProvider(crypto=coin, equity=lb)
    assert (await routed.get_depth("BTC/USD")).source == "coinbase"
    assert (await routed.get_depth("700.HK")).source == "longbridge"
    assert routed.vendors == "longbridge+coinbase"


class _FakeCoinbase:
    name = "coinbase"

    def __init__(self, *, fail=False):
        self.calls, self.fail = [], fail

    async def get_bars(self, symbol, *, bar_interval, start_date, end_date):
        self.calls.append((symbol, bar_interval, start_date))
        if self.fail:
            raise VendorError("coinbase down")
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        return [
            Bar(symbol=symbol, bar_interval=bar_interval, ts=now - timedelta(minutes=5 * k),
                open=Decimal("10"), high=Decimal("11"), low=Decimal("9"),
                close=Decimal("10.5"), volume=None, source="coinbase")
            for k in range(3, -1, -1)
        ]


async def _get(client, token, symbol=SYMBOL):
    today = date.today()
    return await client.get(
        f"/market-data/{symbol}/bars",
        params={"start_date": (today - timedelta(days=2)).isoformat(),
                "end_date": today.isoformat(), "bar_interval": "5m"},
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_an_empty_crypto_chart_is_filled_on_read(fail):
    fake = _FakeCoinbase(fail=fail)
    app.dependency_overrides[get_market_data_bar_backfill_provider] = (
        lambda: BarBackfillRouter(crypto_provider=fake)
    )
    routes.marketdata._last_crypto_refresh.clear()
    try:
        async with db_session() as session, non_admin_user(session) as (_uid, email):
            try:
                async with api_client() as client:
                    token = await _get_token(client, email)
                    first = await _get(client, token)
                    assert first.status_code == 200, first.text
                    # A vendor failure returns what is stored - here nothing -
                    # and never anything invented.
                    assert first.json()["count"] == (0 if fail else 4)
                    second = await _get(client, token)  # throttled: no refetch
                    assert second.status_code == 200
                assert len(fake.calls) == 1
            finally:
                await session.execute(delete(MarketDataBar).where(MarketDataBar.symbol == SYMBOL))
                await session.commit()
    finally:
        app.dependency_overrides.pop(get_market_data_bar_backfill_provider, None)
        routes.marketdata._last_crypto_refresh.clear()


@pytest.mark.asyncio
async def test_an_equity_chart_still_never_contacts_a_vendor():
    fake = _FakeCoinbase()
    app.dependency_overrides[get_market_data_bar_backfill_provider] = (
        lambda: BarBackfillRouter(equity_provider=fake, crypto_provider=fake)
    )
    try:
        async with db_session() as session, non_admin_user(session) as (_uid, email):
            async with api_client() as client:
                token = await _get_token(client, email)
                assert (await _get(client, token, "TESTEQ.US")).json()["count"] == 0
        assert fake.calls == []
    finally:
        app.dependency_overrides.pop(get_market_data_bar_backfill_provider, None)
