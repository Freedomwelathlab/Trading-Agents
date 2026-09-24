"""Broker housekeeping for admins (Phase 97, D116): approve which brokers
the trading desk shows, and delete the unwanted ones without ever deleting
the audit trail.

Against the shared Postgres, like the other broker tests: each test seeds
and cleans up its own rows and never asserts on a global count.
"""

import contextlib
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from apps.api.app.db.models import (
    Broker,
    BrokerAccount,
    BrokerGrant,
    BrokerKind,
    Order,
    OrderStatus,
)
from apps.api.app.risk.models import Side
from tests.api.test_admin import _get_token, admin_user, api_client, db_session, non_admin_user


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@contextlib.asynccontextmanager
async def seeded_broker(session, *, approved: bool = False, grant_to=None, with_order=False):
    broker_id = uuid.uuid4()
    session.add(
        Broker(
            id=broker_id,
            name=f"housekeeping-{broker_id.hex[:6]}",
            kind=BrokerKind.PAPER,
            provider="paper",
            is_active=approved,
        )
    )
    await session.flush()
    if grant_to is not None:
        session.add(BrokerGrant(user_id=grant_to, broker_id=broker_id))
    if with_order:
        session.add(
            Order(
                broker_id=broker_id,
                symbol="AAPL",
                side=Side.BUY,
                quantity=Decimal("1"),
                estimated_price=Decimal("100"),
                status=OrderStatus.FILLED,
            )
        )
    session.add(BrokerAccount(broker_id=broker_id, cash=Decimal("100000")))
    await session.commit()
    try:
        yield broker_id
    finally:
        await session.rollback()
        for model in (Order, BrokerAccount, BrokerGrant):
            await session.execute(delete(model).where(model.broker_id == broker_id))
        await session.execute(delete(Broker).where(Broker.id == broker_id))
        await session.commit()


@pytest.mark.asyncio
async def test_the_desk_lists_only_approved_brokers_when_it_asks():
    async with db_session() as session, admin_user(session) as (uid, email):
        async with (
            seeded_broker(session, approved=True, grant_to=uid) as shown,
            seeded_broker(session, approved=False, grant_to=uid) as hidden,
            api_client() as client,
        ):
            token = await _get_token(client, email)
            desk = (await client.get("/brokers?approved_only=true", headers=_h(token))).json()
            everything = (await client.get("/brokers", headers=_h(token))).json()

    desk_ids = {b["id"] for b in desk["brokers"]}
    all_ids = {b["id"] for b in everything["brokers"]}
    assert str(shown) in desk_ids and str(hidden) not in desk_ids
    # Every other caller keeps the unfiltered list.
    assert {str(shown), str(hidden)} <= all_ids


@pytest.mark.asyncio
async def test_approval_flips_what_the_desk_shows_and_reports_unknown_ids():
    missing = uuid.uuid4()
    async with db_session() as session, admin_user(session) as (uid, email):
        async with seeded_broker(session, grant_to=uid) as broker_id, api_client() as client:
            token = await _get_token(client, email)
            r = await client.post(
                "/admin/brokers/approval",
                json={"broker_ids": [str(broker_id), str(missing)], "approved": True},
                headers=_h(token),
            )
            listing = (await client.get("/admin/brokers", headers=_h(token))).json()

    assert r.status_code == 200, r.text
    assert r.json() == {"updated": 1, "not_found": [str(missing)]}
    row = next(b for b in listing["brokers"] if b["id"] == str(broker_id))
    assert row["approved"] is True
    assert row["adapter_status"] == "built_in"
    assert row["order_count"] == 0


@pytest.mark.asyncio
async def test_delete_removes_an_unused_broker_and_archives_one_with_history():
    async with db_session() as session, admin_user(session) as (uid, email):
        async with (
            seeded_broker(session, approved=True, grant_to=uid) as unused,
            seeded_broker(session, approved=True, grant_to=uid, with_order=True) as traded,
            api_client() as client,
        ):
            token = await _get_token(client, email)
            r = await client.post(
                "/admin/brokers/delete",
                json={"broker_ids": [str(unused), str(traded)]},
                headers=_h(token),
            )
            assert r.status_code == 200, r.text
            outcomes = {o["broker_id"]: o for o in r.json()["results"]}

            gone = (
                await session.execute(select(Broker.id).where(Broker.id == unused))
            ).scalar_one_or_none()
            kept_active = (
                await session.execute(select(Broker.is_active).where(Broker.id == traded))
            ).scalar_one()
            orders_kept = (
                await session.execute(select(Order.id).where(Order.broker_id == traded))
            ).scalars().all()

    assert outcomes[str(unused)]["outcome"] == "deleted"
    assert gone is None
    assert outcomes[str(traded)]["outcome"] == "archived"
    assert "audit trail" in outcomes[str(traded)]["detail"]
    assert kept_active is False  # hidden from the desk
    assert len(orders_kept) == 1  # the order history is never deleted


@pytest.mark.asyncio
async def test_an_unknown_id_is_reported_not_fatal():
    missing = uuid.uuid4()
    async with db_session() as session, admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            r = await client.post(
                "/admin/brokers/delete", json={"broker_ids": [str(missing)]}, headers=_h(token)
            )
    assert r.json()["results"][0]["outcome"] == "not_found"


@pytest.mark.asyncio
async def test_housekeeping_is_admin_only():
    async with db_session() as session, non_admin_user(session) as (_uid, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            listing = await client.get("/admin/brokers", headers=_h(token))
            deleting = await client.post(
                "/admin/brokers/delete",
                json={"broker_ids": [str(uuid.uuid4())]},
                headers=_h(token),
            )
    assert listing.status_code == 403
    assert deleting.status_code == 403
