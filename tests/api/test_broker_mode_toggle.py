"""The per-broker paper/live trading-mode toggle (Phase 43, D058).

A broker row's `kind` IS the switch. There is ONE trade-submission
endpoint, `POST /brokers/{broker_id}/trades`, and the broker's kind - not
anything in the request body - decides whether the order is routed to
`PaperBrokerAdapter` or to `LiveBrokerAdapter`. These tests cover both
halves of that claim:

  * the admin surface that creates a broker with a kind and flips an
    existing broker between kinds, including the guards on flipping, and
  * the routing consequence: the SAME request against the SAME endpoint
    reaches the paper simulator or the live adapter purely according to
    the broker row.

As everywhere else in this phase, no real broker is contacted and
LIVE_TRADING_ENABLED is never set to true. The live side is a real
`LiveBrokerAdapter` driven by the fake SDK client from
tests/execution/test_live_broker.py, installed via FastAPI's
dependency_overrides.
"""

import uuid

import pytest
from sqlalchemy import delete, select

from apps.api.app.auth.permissions import Permission
from apps.api.app.db.models import Broker, BrokerAccount, BrokerKind
from apps.api.app.db.models import Order as OrderRow
from apps.api.app.execution.live_broker import LiveBrokerAdapter
from tests.api.test_live_trades import (
    BOTH_TRADE_PERMISSIONS,
    _live_adapter,
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
from tests.execution.test_live_broker import FakeLiveTradeClient

ADMIN_AND_TRADE = (
    Permission.ADMIN.value,
    Permission.SUBMIT_PAPER_TRADE.value,
    Permission.SUBMIT_LIVE_TRADE.value,
)

TRADE_PAYLOAD = {
    "symbol": "AAPL",
    "side": "buy",
    "quantity": "10",
    "estimated_price": "100",
    "stop_price": "95",
}


async def _cleanup_broker(session, broker_id: uuid.UUID) -> None:
    await session.execute(delete(BrokerAccount).where(BrokerAccount.broker_id == broker_id))
    await session.execute(delete(Broker).where(Broker.id == broker_id))
    await session.commit()


# --- creating a broker with an explicit kind ----------------------------


@pytest.mark.asyncio
async def test_an_admin_can_create_a_paper_broker():
    async with db_session() as session, active_user(session, permissions=ADMIN_AND_TRADE) as (
        _user_id,
        email,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                "/admin/brokers",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Sim One", "kind": "paper", "provider": "paper-sim"},
            )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["kind"] == "paper"
        await _cleanup_broker(session, uuid.UUID(body["id"]))


@pytest.mark.asyncio
async def test_creating_a_live_broker_requires_explicit_confirmation():
    async with db_session() as session, active_user(session, permissions=ADMIN_AND_TRADE) as (
        _user_id,
        email,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                "/admin/brokers",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Real One", "kind": "live", "provider": "longbridge"},
            )
        assert response.status_code == 400
        assert "LIVE_KIND_CONFIRMATION_REQUIRED" in response.json()["detail"]

        created = (
            await session.execute(select(Broker).where(Broker.name == "Real One"))
        ).scalars().all()
        assert created == []


@pytest.mark.asyncio
async def test_a_confirmed_live_broker_can_be_created():
    async with db_session() as session, active_user(session, permissions=ADMIN_AND_TRADE) as (
        _user_id,
        email,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                "/admin/brokers",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "name": "Real One",
                    "kind": "live",
                    "provider": "longbridge",
                    "confirm_live": True,
                },
            )
        assert response.status_code == 201, response.text
        assert response.json()["kind"] == "live"
        await _cleanup_broker(session, uuid.UUID(response.json()["id"]))


@pytest.mark.asyncio
async def test_creating_a_broker_requires_the_admin_permission():
    async with db_session() as session, active_user(session) as (_user_id, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                "/admin/brokers",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": "Sim One", "kind": "paper", "provider": "paper-sim"},
            )
    assert response.status_code == 403


# --- flipping an existing broker ----------------------------------------


@pytest.mark.asyncio
async def test_an_admin_can_flip_an_untraded_broker_from_paper_to_live():
    async with (
        db_session() as session,
        active_user(session, permissions=ADMIN_AND_TRADE) as (_user_id, email),
        paper_broker_row(session) as broker_id,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.patch(
                f"/admin/brokers/{broker_id}/mode",
                headers={"Authorization": f"Bearer {token}"},
                json={"kind": "live", "confirm_live": True},
            )
        assert response.status_code == 200, response.text
        assert response.json()["kind"] == "live"

        broker = (
            await session.execute(select(Broker).where(Broker.id == broker_id))
        ).scalar_one()
        await session.refresh(broker)
        assert broker.kind is BrokerKind.LIVE


@pytest.mark.asyncio
async def test_flipping_to_live_without_confirmation_is_refused_and_changes_nothing():
    async with (
        db_session() as session,
        active_user(session, permissions=ADMIN_AND_TRADE) as (_user_id, email),
        paper_broker_row(session) as broker_id,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.patch(
                f"/admin/brokers/{broker_id}/mode",
                headers={"Authorization": f"Bearer {token}"},
                json={"kind": "live"},
            )
        assert response.status_code == 400
        assert "LIVE_KIND_CONFIRMATION_REQUIRED" in response.json()["detail"]

        broker = (
            await session.execute(select(Broker).where(Broker.id == broker_id))
        ).scalar_one()
        await session.refresh(broker)
        assert broker.kind is BrokerKind.PAPER


@pytest.mark.asyncio
async def test_flipping_live_to_paper_needs_no_confirmation():
    """Only the direction that moves toward real money is made awkward."""
    async with (
        db_session() as session,
        active_user(session, permissions=ADMIN_AND_TRADE) as (_user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.patch(
                f"/admin/brokers/{broker_id}/mode",
                headers={"Authorization": f"Bearer {token}"},
                json={"kind": "paper"},
            )
        assert response.status_code == 200, response.text
        assert response.json()["kind"] == "paper"


@pytest.mark.asyncio
async def test_a_broker_with_recorded_orders_can_no_longer_be_flipped():
    """The audit-integrity guard: orders/fills record no per-order kind, so
    a flipped broker's history would become ambiguous."""
    async with (
        db_session() as session,
        active_user(session, permissions=ADMIN_AND_TRADE) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            traded = await client.post(
                f"/brokers/{broker_id}/trades", headers=headers, json=TRADE_PAYLOAD
            )
            assert traded.status_code == 200

            response = await client.patch(
                f"/admin/brokers/{broker_id}/mode",
                headers=headers,
                json={"kind": "live", "confirm_live": True},
            )

        assert response.status_code == 409
        assert "recorded orders" in response.json()["detail"]

        broker = (
            await session.execute(select(Broker).where(Broker.id == broker_id))
        ).scalar_one()
        await session.refresh(broker)
        assert broker.kind is BrokerKind.PAPER


@pytest.mark.asyncio
async def test_a_broker_with_a_simulated_book_but_no_orders_cannot_be_flipped_either():
    async with (
        db_session() as session,
        active_user(session, permissions=ADMIN_AND_TRADE) as (_user_id, email),
        paper_broker_row(session) as broker_id,
    ):
        session.add(BrokerAccount(broker_id=broker_id, cash=100000))
        await session.commit()

        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.patch(
                f"/admin/brokers/{broker_id}/mode",
                headers={"Authorization": f"Bearer {token}"},
                json={"kind": "live", "confirm_live": True},
            )

        assert response.status_code == 409
        assert "simulated cash/position book" in response.json()["detail"]
        await session.execute(delete(BrokerAccount).where(BrokerAccount.broker_id == broker_id))
        await session.commit()


@pytest.mark.asyncio
async def test_a_no_op_flip_succeeds_even_on_a_traded_broker():
    """Requesting the kind a broker already has changes nothing, so the
    guards have nothing to protect."""
    async with (
        db_session() as session,
        active_user(session, permissions=ADMIN_AND_TRADE) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            await client.post(f"/brokers/{broker_id}/trades", headers=headers, json=TRADE_PAYLOAD)
            response = await client.patch(
                f"/admin/brokers/{broker_id}/mode", headers=headers, json={"kind": "paper"}
            )
    assert response.status_code == 200
    assert response.json()["kind"] == "paper"


@pytest.mark.asyncio
async def test_flipping_an_unknown_broker_is_404():
    async with db_session() as session, active_user(session, permissions=ADMIN_AND_TRADE) as (
        _user_id,
        email,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.patch(
                f"/admin/brokers/{uuid.uuid4()}/mode",
                headers={"Authorization": f"Bearer {token}"},
                json={"kind": "paper"},
            )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_flipping_a_broker_requires_the_admin_permission():
    async with (
        db_session() as session,
        active_user(session) as (_user_id, email),
        paper_broker_row(session) as broker_id,
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.patch(
                f"/admin/brokers/{broker_id}/mode",
                headers={"Authorization": f"Bearer {token}"},
                json={"kind": "live", "confirm_live": True},
            )
    assert response.status_code == 403


# --- the routing consequence: the toggle actually switches adapters -----


@pytest.mark.asyncio
async def test_the_same_endpoint_routes_to_the_paper_simulator_when_kind_is_paper():
    exploding = FakeLiveTradeClient()

    class ExplodingClient(FakeLiveTradeClient):
        def account_balance(self, currency: str | None = None):
            raise AssertionError("a paper-kind broker must not reach the live adapter")

    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            with _override_live_broker(
                LiveBrokerAdapter(ExplodingClient())  # type: ignore[arg-type]
            ):
                response = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={**TRADE_PAYLOAD, "confirm": True},
                )

        assert response.status_code == 200
        assert response.json()["status"] == "filled"
        # The paper simulator's book was written; the live client was not touched.
        account = (
            await session.execute(
                select(BrokerAccount).where(BrokerAccount.broker_id == broker_id)
            )
        ).scalar_one()
        assert account.cash == 99000
        assert exploding.submitted == []


@pytest.mark.asyncio
async def test_flipping_a_broker_to_live_switches_which_adapter_the_same_request_reaches():
    """The end-to-end statement of the toggle: one broker, one endpoint,
    one payload; flipping `kind` is the only thing that changes, and it is
    what decides which adapter executes the order."""
    live_client = FakeLiveTradeClient()

    async with (
        db_session() as session,
        active_user(session, permissions=ADMIN_AND_TRADE) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}

            flip = await client.patch(
                f"/admin/brokers/{broker_id}/mode",
                headers=headers,
                json={"kind": "live", "confirm_live": True},
            )
            assert flip.status_code == 200, flip.text

            with _override_live_broker(
                LiveBrokerAdapter(live_client)  # type: ignore[arg-type]
            ):
                response = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers=headers,
                    json={**TRADE_PAYLOAD, "confirm": True},
                )

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "filled"
        # The LIVE adapter executed it...
        assert len(live_client.submitted) == 1
        assert live_client.submitted[0]["symbol"] == "AAPL"
        # ...and no simulated book was created for it.
        accounts = (
            await session.execute(
                select(BrokerAccount).where(BrokerAccount.broker_id == broker_id)
            )
        ).scalars().all()
        assert accounts == []
        # The order row IS recorded for a live trade (it is this system's
        # own audit record of what it did) - the paper *book* is what is
        # absent. paper_broker_row's teardown removes it.
        orders = (
            await session.execute(select(OrderRow).where(OrderRow.broker_id == broker_id))
        ).scalars().all()
        assert len(orders) == 1


@pytest.mark.asyncio
async def test_a_live_kind_broker_ignores_no_body_field_that_could_force_paper_execution():
    """There is deliberately no request-body field that selects the
    execution mode: `confirm` gates a live trade, it does not choose one.
    A live-kind broker with an unconfigured live path is refused outright
    rather than falling back to the simulator."""
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                f"/brokers/{broker_id}/trades",
                headers={"Authorization": f"Bearer {token}"},
                json={**TRADE_PAYLOAD, "confirm": True},
            )

        assert response.status_code == 400
        assert "NOT_CONFIGURED" in response.json()["detail"]
        accounts = (
            await session.execute(
                select(BrokerAccount).where(BrokerAccount.broker_id == broker_id)
            )
        ).scalars().all()
        assert accounts == []


@pytest.mark.asyncio
async def test_the_broker_listing_reports_the_current_mode():
    """A caller can read a broker's current execution mode back - the
    toggle is observable, not just settable (GET /brokers/{id}, D034)."""
    async with (
        db_session() as session,
        active_user(session, permissions=ADMIN_AND_TRADE) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}

            before = await client.get(f"/brokers/{broker_id}", headers=headers)
            assert before.json()["kind"] == "paper"

            await client.patch(
                f"/admin/brokers/{broker_id}/mode",
                headers=headers,
                json={"kind": "live", "confirm_live": True},
            )
            after = await client.get(f"/brokers/{broker_id}", headers=headers)

        assert after.status_code == 200
        assert after.json()["kind"] == "live"


@pytest.mark.asyncio
async def test_a_live_adapter_double_is_never_reached_by_a_paper_broker_after_a_flip_back():
    """Flipping back to paper must restore paper routing completely."""
    async with (
        db_session() as session,
        active_user(session, permissions=ADMIN_AND_TRADE) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            await client.patch(
                f"/admin/brokers/{broker_id}/mode", headers=headers, json={"kind": "paper"}
            )
            with _override_live_broker(_live_adapter()):
                response = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers=headers,
                    json={**TRADE_PAYLOAD, "confirm": True},
                )

        assert response.status_code == 200
        assert response.json()["status"] == "filled"
        account = (
            await session.execute(
                select(BrokerAccount).where(BrokerAccount.broker_id == broker_id)
            )
        ).scalar_one()
        assert account.cash == 99000
