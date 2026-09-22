"""Limit orders and cancel (Phase 84, D101).

Paper: a marketable limit fills at the market; a non-marketable one is a
409, never a pretend resting order. Live (fake client): a limit order goes
to the venue as `LO` with its price; an unexecuted one is recorded
`submitted_unconfirmed`; cancel asks the venue and records what the venue
says — including a fill that raced the cancel.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from apps.api.app.db.models import BrokerKind
from apps.api.app.db.models import Order as OrderRow
from apps.api.app.execution.live_broker import LiveBrokerAdapter
from tests.api.test_live_trades import (
    BOTH_TRADE_PERMISSIONS,
    _override_live_broker,
)
from tests.api.test_trades import (
    _get_token,
    active_user,
    api_client,
    broker_grant,
    db_session,
    paper_broker_row,
)
from tests.execution.test_live_broker import FakeLiveTradeClient, _OrderDetail


class CancellableFakeClient(FakeLiveTradeClient):
    """The fake client plus `cancel_order`, which flips the reported
    status to whatever the test says the venue answers."""

    def __init__(self, *, after_cancel: _OrderDetail, **kw):
        super().__init__(**kw)
        self._after_cancel = after_cancel
        self.cancelled: list[str] = []

    def cancel_order(self, order_id: str) -> None:
        self.cancelled.append(order_id)
        self._detail = self._after_cancel


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_a_marketable_paper_limit_fills_at_the_market_price():
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
        api_client() as client,
    ):
        token = await _get_token(client, email)
        r = await client.post(
            f"/brokers/{broker_id}/trades", headers=_h(token),
            json={
                "symbol": "AAPL", "side": "buy", "quantity": "10",
                "order_type": "limit", "limit_price": "105",
                "estimated_price": "100", "stop_price": "95",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "filled"
        # Sized/filled on the quote (100), not the limit (105).
        assert Decimal(body["fill_price"]) == Decimal("100")


@pytest.mark.asyncio
async def test_a_non_marketable_paper_limit_is_refused_not_rested():
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
        api_client() as client,
    ):
        token = await _get_token(client, email)
        r = await client.post(
            f"/brokers/{broker_id}/trades", headers=_h(token),
            json={
                "symbol": "AAPL", "side": "buy", "quantity": "10",
                "order_type": "limit", "limit_price": "90",
                "estimated_price": "100", "stop_price": "85",
            },
        )
        assert r.status_code == 409, r.text
        assert "ORDER_WOULD_REST" in r.json()["detail"]
        rows = (
            await session.execute(select(OrderRow).where(OrderRow.broker_id == broker_id))
        ).scalars().all()
        assert rows == []  # nothing recorded: no order existed anywhere


@pytest.mark.asyncio
async def test_a_limit_without_a_price_is_400():
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
        api_client() as client,
    ):
        token = await _get_token(client, email)
        r = await client.post(
            f"/brokers/{broker_id}/trades", headers=_h(token),
            json={"symbol": "AAPL", "side": "buy", "quantity": "1", "order_type": "limit",
                  "estimated_price": "100"},
        )
        assert r.status_code == 400 and "LIMIT_PRICE_REQUIRED" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_live_limit_goes_to_the_venue_as_lo_and_can_be_cancelled():
    resting = _OrderDetail(status="New", executed_quantity="0", executed_price=None)
    cancelled = _OrderDetail(status="Canceled", executed_quantity="0", executed_price=None)
    fake = CancellableFakeClient(detail=resting, after_cancel=cancelled)
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
        api_client() as client,
    ):
        token = await _get_token(client, email)
        with _override_live_broker(LiveBrokerAdapter(fake)):  # type: ignore[arg-type]
            r = await client.post(
                f"/brokers/{broker_id}/trades", headers=_h(token),
                json={
                    "symbol": "AAPL", "side": "buy", "quantity": "10",
                    "order_type": "limit", "limit_price": "95",
                    "estimated_price": "100", "stop_price": "90", "confirm": True,
                },
            )
            # The venue accepted but did not execute: a real order exists.
            assert r.status_code == 502, r.text
            assert "LIVE_ORDER_UNCONFIRMED" in r.json()["detail"]
            sent = fake.submitted[-1]
            assert getattr(sent["order_type"], "name", str(sent["order_type"])).endswith("LO")
            assert sent["submitted_price"] == Decimal("95")

            row = (
                await session.execute(select(OrderRow).where(OrderRow.broker_id == broker_id))
            ).scalar_one()
            assert row.status.value == "submitted_unconfirmed"
            assert row.broker_order_id == "LB-ORDER-1"

            r = await client.post(
                f"/brokers/{broker_id}/orders/{row.id}/cancel", headers=_h(token)
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert fake.cancelled == ["LB-ORDER-1"]
            assert body["outcome"] == "cancelled"
            assert body["order"]["status"] == "broker_closed_unfilled"
            assert body["broker_status"] == "Canceled"

            # A second cancel finds nothing cancellable.
            r = await client.post(
                f"/brokers/{broker_id}/orders/{row.id}/cancel", headers=_h(token)
            )
            assert r.status_code == 409


@pytest.mark.asyncio
async def test_a_cancel_that_raced_a_fill_records_the_fill():
    resting = _OrderDetail(status="New", executed_quantity="0", executed_price=None)
    filled = _OrderDetail(status="Filled", executed_quantity="10", executed_price="94.5")
    fake = CancellableFakeClient(detail=resting, after_cancel=filled)
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
        api_client() as client,
    ):
        token = await _get_token(client, email)
        with _override_live_broker(LiveBrokerAdapter(fake)):  # type: ignore[arg-type]
            await client.post(
                f"/brokers/{broker_id}/trades", headers=_h(token),
                json={
                    "symbol": "AAPL", "side": "buy", "quantity": "10",
                    "order_type": "limit", "limit_price": "95",
                    "estimated_price": "100", "stop_price": "90", "confirm": True,
                },
            )
            row = (
                await session.execute(select(OrderRow).where(OrderRow.broker_id == broker_id))
            ).scalar_one()
            r = await client.post(
                f"/brokers/{broker_id}/orders/{row.id}/cancel", headers=_h(token)
            )
            assert r.status_code == 200
            assert r.json()["outcome"] == "filled"
            assert r.json()["order"]["status"] == "filled"
            assert Decimal(r.json()["order"]["fills"][0]["fill_price"]) == Decimal("94.5")


@pytest.mark.asyncio
async def test_a_paper_order_is_never_cancellable():
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
        api_client() as client,
    ):
        token = await _get_token(client, email)
        r = await client.post(
            f"/brokers/{broker_id}/trades", headers=_h(token),
            json={"symbol": "AAPL", "side": "buy", "quantity": "1",
                  "estimated_price": "100", "stop_price": "95"},
        )
        order_id = r.json()["order_id"]
        r = await client.post(f"/brokers/{broker_id}/orders/{order_id}/cancel", headers=_h(token))
        assert r.status_code == 409 and "NOT_CANCELLABLE" in r.json()["detail"]
