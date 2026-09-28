"""The option snapshot routes (Phase 100, D119).

Capture WRITES to the database, so it is `admin:manage`; reading is
market data, so any authenticated user. A day with no snapshot is a 404
DATA_UNAVAILABLE, never the nearest day's chain. The vendor is the stub
from tests/options/test_snapshots.py, swapped on `app.state` so the real
dependency, route and response models run.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import delete

from apps.api.app.db.models import OptionChainSnapshot
from apps.api.app.marketdata.providers.cboe import today_in_new_york
from tests.api.test_admin import _get_token, admin_user, api_client, db_session, non_admin_user
from tests.api.test_option_chain_routes import provider
from tests.options.test_snapshots import UNDERLYING, StubProvider

PATH = f"/admin/options/snapshots/{UNDERLYING}"


def live_stub() -> StubProvider:
    """The route captures at the REAL clock, so the stub's expiries are
    relative to today - fixed dates would silently fall out of the 60-DTE
    window as the calendar moves and turn this test into a time bomb."""
    today = today_in_new_york()
    return StubProvider(expiries=[today + timedelta(days=7), today + timedelta(days=21)])


async def _cleanup(session) -> None:
    await session.execute(
        delete(OptionChainSnapshot).where(OptionChainSnapshot.underlying == UNDERLYING)
    )
    await session.commit()


@pytest.mark.asyncio
async def test_a_non_admin_cannot_capture():
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client, provider(live_stub()):
            token = await _get_token(client, email)
            r = await client.post(PATH, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_no_vendor_is_503_not_configured():
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with api_client() as client, provider(None):
            token = await _get_token(client, email)
            r = await client.post(PATH, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503
    assert r.json()["detail"].startswith("NOT_CONFIGURED")


@pytest.mark.asyncio
async def test_an_admin_captures_and_anyone_signed_in_reads_it_back():
    async with db_session() as session, admin_user(session) as (_a, admin_email):
        async with non_admin_user(session) as (_u, user_email):
            try:
                async with api_client() as client, provider(live_stub()):
                    admin = {"Authorization": f"Bearer {await _get_token(client, admin_email)}"}
                    user = {"Authorization": f"Bearer {await _get_token(client, user_email)}"}

                    r = await client.post(PATH, headers=admin)
                    assert r.status_code == 200, r.text
                    body = r.json()
                    assert body["status"] == "captured"
                    assert body["trade_date"] == "2026-09-25"
                    assert body["source"] == "stub-delayed"
                    written = body["rows_written"]
                    assert written > 0

                    r = await client.get(f"/options/snapshots/{UNDERLYING}", headers=user)
                    assert r.status_code == 200, r.text
                    snap = r.json()
                    assert snap["contracts"] == written == len(snap["rows"])
                    assert snap["trade_date"] == "2026-09-25"
                    assert "vendor" in snap["note"]
                    at_90 = [row for row in snap["rows"] if row["strike"] == "90.000000"]
                    assert at_90 and all(row["bid"] is None for row in at_90)

                    r = await client.get(f"/options/snapshots/{UNDERLYING}/dates", headers=user)
                    assert r.json()["dates"] == ["2026-09-25"]

                    r = await client.get(
                        f"/options/snapshots/{UNDERLYING}",
                        params={"date": "2026-09-24"},
                        headers=user,
                    )
                    assert r.status_code == 404
                    assert r.json()["detail"].startswith("DATA_UNAVAILABLE")
            finally:
                await _cleanup(session)


@pytest.mark.asyncio
async def test_reading_requires_authentication():
    async with api_client() as client:
        r = await client.get(f"/options/snapshots/{UNDERLYING}")
    assert r.status_code == 401


def test_the_stub_trading_day_is_the_one_asserted_above():
    assert StubProvider().as_of.date() == date(2026, 9, 25)
