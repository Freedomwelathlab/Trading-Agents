"""FX end to end against real Postgres (Phase 103, D123): an admin backfill
of `*.FX` through the router into `market_data_bars` (IG stubbed - the real
key is suspended), then the Markets endpoints reading it back on the FX
clock."""

from datetime import date, timedelta

import pytest

from apps.api.app.api.dependencies import get_market_data_bar_backfill_provider
from apps.api.app.execution.adapters.ig import IgAdapter
from apps.api.app.main import app
from apps.api.app.marketdata.bar_router import BarBackfillRouter
from apps.api.app.marketdata.providers.ig_prices import IgFxBarProvider
from tests.api.test_admin import _get_token, admin_user, api_client, db_session
from tests.api.test_admin_market_data import clean_up
from tests.marketdata.providers.test_ig_prices import StubIg, _row

SYMBOL = "ZZZYYY.FX"
"""Not a real pair, so the test can never collide with bars an operator
actually ingested - the cleanup deletes every bar for this symbol."""


def _recent_rows():
    # The session-levels endpoint reads the last 10 days, so the stub
    # serves yesterday's London morning (skipping a weekend).
    day = date.today() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    # 09:00 UTC is inside London hours year-round.
    return day, [
        _row(f"{day.isoformat()}T09:{m:02d}:00", 11000.0 + m, 11002.0 + m, 10999.0 + m,
             11001.0 + m)
        for m in range(0, 60, 5)
    ]


@pytest.mark.asyncio
async def test_fx_backfill_stores_mid_bars_and_markets_reads_them_on_the_fx_clock():
    day, rows = _recent_rows()
    fx = IgFxBarProvider(
        IgAdapter(StubIg([rows]), api_key="k", username="u", password="p",
                  account_type="DEMO")
    )
    async with db_session() as session, admin_user(session) as (_uid, email), \
            clean_up(session, SYMBOL), api_client() as client:
        app.dependency_overrides[get_market_data_bar_backfill_provider] = (
            lambda: BarBackfillRouter(fx_provider=fx)
        )
        try:
            token = await _get_token(client, email)
            h = {"Authorization": f"Bearer {token}"}
            r = await client.post(
                "/admin/market-data/backfill", headers=h,
                json={"symbol": SYMBOL.lower(), "bar_interval": "5m",
                      "start_date": day.isoformat(), "end_date": day.isoformat()},
            )
            assert r.status_code == 201, r.text
            assert r.json()["status"] == "succeeded" and r.json()["bars_ingested"] == 12

            r = await client.get(
                f"/market-data/{SYMBOL}/bars", headers=h,
                params={"start_date": day.isoformat(), "end_date": day.isoformat(),
                        "bar_interval": "5m"},
            )
            bars = r.json()["bars"]
            assert len(bars) == 12
            assert all(b["volume"] is None and b["source"] == "ig-mid" for b in bars)
            assert bars[0]["close"] in ("1.100150", "1.10015")

            r = await client.get(f"/market-data/{SYMBOL}/session-levels", headers=h)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["session_calendar"] == "fx_24h"
            assert body["session_date"] == day.isoformat()
            assert body["vwap"] is None  # no volume, no VWAP - never a fabricated one

            r = await client.get(f"/market-data/{SYMBOL}/extended-hours", headers=h)
            assert r.status_code == 422 and "NOT_APPLICABLE" in r.json()["detail"]
            r = await client.get(f"/market-data/{SYMBOL}/signals", headers=h)
            assert r.status_code == 422 and "NOT_SUPPORTED_FOR_FX" in r.json()["detail"]
        finally:
            del app.dependency_overrides[get_market_data_bar_backfill_provider]


@pytest.mark.asyncio
async def test_fx_backfill_without_an_fx_provider_is_a_422_naming_the_setting():
    async with db_session() as session, admin_user(session) as (_uid, email), \
            api_client() as client:
        app.dependency_overrides[get_market_data_bar_backfill_provider] = (
            lambda: BarBackfillRouter()
        )
        try:
            token = await _get_token(client, email)
            r = await client.post(
                "/admin/market-data/backfill",
                headers={"Authorization": f"Bearer {token}"},
                json={"symbol": "EURUSD.FX", "bar_interval": "5m",
                      "start_date": "2025-06-04", "end_date": "2025-06-04"},
            )
        finally:
            del app.dependency_overrides[get_market_data_bar_backfill_provider]
    assert r.status_code == 422
    assert "FX_BAR_PROVIDER=ig" in r.json()["detail"]
