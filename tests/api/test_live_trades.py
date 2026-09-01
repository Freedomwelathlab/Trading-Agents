"""Integration tests for the LIVE trade path (Phase 43, D058), against a
real Postgres instance.

NO REAL BROKER IS EVER CONTACTED, AND LIVE TRADING IS NEVER ENABLED.

Two things make that true and are worth stating plainly, because the whole
point of this file is to prove a money-moving code path without moving any
money:

  1. `LIVE_TRADING_ENABLED` is never set to true anywhere - not in a
     fixture, not in a monkeypatch, not transiently. Every `Settings`
     object here is the repository default. The one place the real
     configuration gate would be satisfied is deliberately never reached;
     instead it is tested in its REFUSING direction
     (`test_a_confirmed_live_trade_is_still_refused_when_no_live_path_is_configured`).
  2. The live execution path is supplied by overriding the
     `get_live_broker_adapter` FastAPI dependency with a REAL
     `LiveBrokerAdapter` driven by the fake SDK client from
     tests/execution/test_live_broker.py. So the adapter under test is the
     production class, exercising its production code, against a double
     that has no network access at all.

What that buys: the confirmation gate, the permission gate, the tighter
live risk limits, the emergency stop, the Portfolio Manager, order/fill
persistence and the "don't write a live book into broker_accounts" rule
are all verified on the actual shared code path, not on a parallel
re-implementation.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from apps.api.app.api.dependencies import get_live_broker_adapter
from apps.api.app.auth.permissions import Permission
from apps.api.app.db.models import Broker, BrokerAccount, BrokerKind, BrokerPosition
from apps.api.app.db.models import Order as OrderRow
from apps.api.app.execution.live_broker import LiveBrokerAdapter
from apps.api.app.main import app
from tests.api.test_trades import (
    _get_token,
    active_user,
    api_client,
    broker_grant,
    db_session,
    paper_broker_row,
)
from tests.execution.test_live_broker import (
    FakeLiveTradeClient,
    _Balance,
    _OrderDetail,
    _Position,
)

BOTH_TRADE_PERMISSIONS = (
    Permission.SUBMIT_PAPER_TRADE.value,
    Permission.SUBMIT_LIVE_TRADE.value,
)


def _live_adapter(**client_kwargs: object) -> LiveBrokerAdapter:
    return LiveBrokerAdapter(FakeLiveTradeClient(**client_kwargs))  # type: ignore[arg-type]


class _override_live_broker:
    """Installs a fake live execution path for the duration of a block.
    Uses FastAPI's dependency_overrides, so no Settings value is touched
    and LIVE_TRADING_ENABLED stays false throughout."""

    def __init__(self, adapter: object | None) -> None:
        self._adapter = adapter

    def __enter__(self) -> None:
        app.dependency_overrides[get_live_broker_adapter] = lambda: self._adapter

    def __exit__(self, *exc: object) -> None:
        del app.dependency_overrides[get_live_broker_adapter]


# --- the confirmation gate ----------------------------------------------


@pytest.mark.asyncio
async def test_a_live_trade_without_confirm_is_refused():
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            with _override_live_broker(_live_adapter()):
                response = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "AAPL",
                        "side": "buy",
                        "quantity": "10",
                        "estimated_price": "100",
                        "stop_price": "95",
                    },
                )

    assert response.status_code == 400
    assert "LIVE_CONFIRMATION_REQUIRED" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_live_trade_with_confirm_false_is_refused_exactly_like_an_omitted_one():
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            with _override_live_broker(_live_adapter()):
                response = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "AAPL",
                        "side": "buy",
                        "quantity": "10",
                        "estimated_price": "100",
                        "stop_price": "95",
                        "confirm": False,
                    },
                )

    assert response.status_code == 400
    assert "LIVE_CONFIRMATION_REQUIRED" in response.json()["detail"]


@pytest.mark.asyncio
async def test_an_unconfirmed_live_trade_reaches_no_broker_and_records_no_order():
    client_double = FakeLiveTradeClient()
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            with _override_live_broker(LiveBrokerAdapter(client_double)):  # type: ignore[arg-type]
                await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "AAPL",
                        "side": "buy",
                        "quantity": "10",
                        "estimated_price": "100",
                        "stop_price": "95",
                    },
                )

        assert client_double.submitted == []
        orders = (
            await session.execute(select(OrderRow).where(OrderRow.broker_id == broker_id))
        ).scalars().all()
        assert orders == []


# --- the configuration gate (tested only in its refusing direction) ------


@pytest.mark.asyncio
async def test_a_confirmed_live_trade_is_still_refused_when_no_live_path_is_configured():
    """The repository default. Nothing is overridden here, so the real
    `get_live_broker_adapter` runs and returns None because
    LIVE_TRADING_ENABLED is false - which is exactly the state this phase
    must leave the system in."""
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
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "100",
                    "stop_price": "95",
                    "confirm": True,
                },
            )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "NOT_CONFIGURED" in detail
    assert "LIVE_TRADING_ENABLED=true" in detail


# --- the permission gate ------------------------------------------------


@pytest.mark.asyncio
async def test_paper_trade_permission_alone_does_not_authorize_a_live_trade():
    async with (
        db_session() as session,
        active_user(session) as (user_id, email),  # paper permission only
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            with _override_live_broker(_live_adapter()):
                response = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "AAPL",
                        "side": "buy",
                        "quantity": "10",
                        "estimated_price": "100",
                        "stop_price": "95",
                        "confirm": True,
                    },
                )

    assert response.status_code == 403
    assert "trade:submit:live" in response.json()["detail"]


# --- the happy path, against the fake broker double ---------------------


@pytest.mark.asyncio
async def test_a_fully_gated_live_trade_fills_with_the_brokers_own_price():
    adapter = _live_adapter(
        balances=[_Balance("USD", "100000")],
        detail=_OrderDetail(status="Filled", executed_quantity="10", executed_price="100.75"),
    )
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            with _override_live_broker(adapter):
                response = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "AAPL",
                        "side": "buy",
                        "quantity": "10",
                        "estimated_price": "100",
                        "stop_price": "95",
                        "confirm": True,
                    },
                )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "filled"
        assert body["approved"] is True
        # The BROKER's executed price, not the caller's estimate.
        assert body["fill_price"] == "100.75"

        order = (
            await session.execute(select(OrderRow).where(OrderRow.broker_id == broker_id))
        ).scalar_one()
        assert order.submitted_by_user_id == user_id


@pytest.mark.asyncio
async def test_a_live_trade_writes_no_paper_account_or_position_rows():
    """A live account's cash and positions live at the venue. Writing them
    into broker_accounts/broker_positions would create a second book that
    diverges the moment anything happens outside this process."""
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            with _override_live_broker(_live_adapter()):
                response = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "AAPL",
                        "side": "buy",
                        "quantity": "10",
                        "estimated_price": "100",
                        "stop_price": "95",
                        "confirm": True,
                    },
                )
        assert response.status_code == 200, response.text

        accounts = (
            await session.execute(
                select(BrokerAccount).where(BrokerAccount.broker_id == broker_id)
            )
        ).scalars().all()
        positions = (
            await session.execute(
                select(BrokerPosition).where(BrokerPosition.broker_id == broker_id)
            )
        ).scalars().all()
        assert accounts == []
        assert positions == []


@pytest.mark.asyncio
async def test_an_accepted_but_unexecuted_live_order_is_surfaced_not_faked():
    adapter = _live_adapter(
        detail=_OrderDetail(status="New", executed_quantity="0", executed_price=None)
    )
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            with _override_live_broker(adapter):
                response = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "AAPL",
                        "side": "buy",
                        "quantity": "10",
                        "estimated_price": "100",
                        "stop_price": "95",
                        "confirm": True,
                    },
                )

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "LIVE_ORDER_UNCONFIRMED" in detail
    assert "LB-ORDER-1" in detail


# --- the tighter live risk limits ---------------------------------------


@pytest.mark.asyncio
async def test_a_size_that_paper_would_allow_is_blocked_by_the_tighter_live_limit():
    """6,000 notional on 100,000 of equity is 6% - inside paper's 10% cap
    and outside live's 5% one. The SAME Risk Engine rejects it; only the
    limits differ."""
    adapter = _live_adapter(balances=[_Balance("USD", "100000")])
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            with _override_live_broker(adapter):
                response = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "AAPL",
                        "side": "buy",
                        "quantity": "10",
                        "estimated_price": "600",
                        "stop_price": "595",
                        "confirm": True,
                    },
                )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert body["block_reason"] == "exceeds_max_position_size"
    assert body["fill_price"] is None


@pytest.mark.asyncio
async def test_the_same_size_is_accepted_on_a_paper_broker():
    """The control for the test above - identical payload, paper broker,
    paper limits, approved. This is what 'the live limits are separate'
    means concretely."""
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            response = await client.post(
                f"/brokers/{broker_id}/trades",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "600",
                    "stop_price": "595",
                },
            )

    assert response.status_code == 200
    assert response.json()["status"] == "filled"


# --- the shared gates still gate a live trade (requirement 4) -----------


@pytest.mark.asyncio
async def test_the_emergency_stop_blocks_a_fully_confirmed_live_trade():
    from apps.api.app.core.config import get_settings

    client_double = FakeLiveTradeClient()
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        settings = get_settings()
        original = settings.emergency_stop_active
        settings.emergency_stop_active = True
        try:
            async with api_client() as client:
                token = await _get_token(client, email)
                with _override_live_broker(
                    LiveBrokerAdapter(client_double)  # type: ignore[arg-type]
                ):
                    response = await client.post(
                        f"/brokers/{broker_id}/trades",
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "symbol": "AAPL",
                            "side": "buy",
                            "quantity": "10",
                            "estimated_price": "100",
                            "stop_price": "95",
                            "confirm": True,
                        },
                    )
        finally:
            settings.emergency_stop_active = original

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert body["block_reason"] == "emergency_stop_active"
    # The decisive assertion: no order reached the broker.
    assert client_double.submitted == []


@pytest.mark.asyncio
async def test_the_portfolio_manager_gates_a_fully_confirmed_live_trade():
    """The live account already holds the Portfolio Manager's maximum of 20
    distinct symbols, so a 21st is rejected by the SAME deterministic
    manager a paper trade goes through (D029) - the Risk Engine approves
    this trade first (it is tiny: 1,000 notional and 3,000 total exposure
    against 100,000 of equity, inside even the tightened live 5%/20%
    caps), and the Portfolio Manager is the layer that stops it.

    `max_open_positions` rather than the per-symbol concentration cap
    because, with live exposure capped at 20% of equity, no single symbol
    can reach the 25% concentration cap at all - the tighter live limit
    above it always binds first. That interaction is itself worth knowing
    and is recorded in D058."""
    held = [_Position(f"SYM{i}", "1") for i in range(20)]
    marks = {f"SYM{i}": "100" for i in range(20)}
    adapter = _live_adapter(balances=[_Balance("USD", "98000")], positions=held)
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            with _override_live_broker(adapter):
                response = await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "NEWCO",
                        "side": "buy",
                        "quantity": "10",
                        "estimated_price": "100",
                        "stop_price": "95",
                        "marks": marks,
                        "confirm": True,
                    },
                )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approved"] is True, "the Risk Engine should have passed this one"
    assert body["status"] == "rejected"
    assert body["portfolio_action"] == "reject"
    assert body["portfolio_binding_constraint"] == "max_open_positions"


@pytest.mark.asyncio
async def test_a_duplicate_live_order_is_blocked_by_the_same_duplicate_check():
    adapter = _live_adapter()
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            payload = {
                "symbol": "AAPL",
                "side": "buy",
                "quantity": "10",
                "estimated_price": "100",
                "stop_price": "95",
                "confirm": True,
            }
            headers = {"Authorization": f"Bearer {token}"}
            with _override_live_broker(adapter):
                first = await client.post(
                    f"/brokers/{broker_id}/trades", headers=headers, json=payload
                )
                second = await client.post(
                    f"/brokers/{broker_id}/trades", headers=headers, json=payload
                )

    assert first.status_code == 200 and first.json()["status"] == "filled"
    assert second.status_code == 200
    assert second.json()["block_reason"] == "duplicate_order"


# --- the paper path is untouched ----------------------------------------


@pytest.mark.asyncio
async def test_a_paper_trade_never_reaches_the_live_broker_even_when_one_is_configured():
    class ExplodingClient(FakeLiveTradeClient):
        def submit_order(self, **kwargs: object):
            raise AssertionError("a paper trade must never reach the live broker")

        def account_balance(self, currency: str | None = None):
            raise AssertionError("a paper trade must never read the live account")

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
                    json={
                        "symbol": "AAPL",
                        "side": "buy",
                        "quantity": "10",
                        "estimated_price": "100",
                        "stop_price": "95",
                        # Sent deliberately: `confirm` must be inert on a
                        # paper broker, not a switch that changes anything.
                        "confirm": True,
                    },
                )

        assert response.status_code == 200
        assert response.json()["status"] == "filled"

        account = (
            await session.execute(
                select(BrokerAccount).where(BrokerAccount.broker_id == broker_id)
            )
        ).scalar_one()
        assert account.cash == Decimal("99000")


@pytest.mark.asyncio
async def test_the_agent_route_still_refuses_a_live_broker_outright():
    """No `confirm` field exists on AgentTradeRequest and none should: an
    agent-chosen side/quantity is not something a human confirmed."""
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            with _override_live_broker(_live_adapter()):
                response = await client.post(
                    f"/brokers/{broker_id}/agent-trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"symbol": "AAPL", "directive": "buy a little"},
                )

    assert response.status_code == 400
    assert "NOT_CONFIGURED" in response.json()["detail"]


@pytest.mark.asyncio
async def test_an_unknown_live_broker_id_is_still_a_404_before_any_live_logic():
    async with db_session() as session, active_user(
        session, permissions=BOTH_TRADE_PERMISSIONS
    ) as (_user_id, email):
        async with api_client() as client:
            token = await _get_token(client, email)
            with _override_live_broker(_live_adapter()):
                response = await client.post(
                    f"/brokers/{uuid.uuid4()}/trades",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "symbol": "AAPL",
                        "side": "buy",
                        "quantity": "10",
                        "estimated_price": "100",
                        "stop_price": "95",
                        "confirm": True,
                    },
                )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_stray_broker_account_row_is_never_created_for_a_live_broker():
    """Guards the specific regression this phase could have introduced:
    `load_paper_broker` CREATES a broker_accounts row as a side effect, so
    running it for a live broker would silently mint a fake 100,000 paper
    account beside a real one."""
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await _get_token(client, email)
            with _override_live_broker(_live_adapter()):
                # Exercise several outcomes: refused, rejected, filled.
                headers = {"Authorization": f"Bearer {token}"}
                base = {
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "100",
                    "stop_price": "95",
                }
                await client.post(f"/brokers/{broker_id}/trades", headers=headers, json=base)
                await client.post(
                    f"/brokers/{broker_id}/trades",
                    headers=headers,
                    json={**base, "quantity": "100000", "confirm": True},
                )
                await client.post(
                    f"/brokers/{broker_id}/trades", headers=headers, json={**base, "confirm": True}
                )

        rows = (
            await session.execute(
                select(Broker.id).join(
                    BrokerAccount, BrokerAccount.broker_id == Broker.id
                ).where(Broker.id == broker_id)
            )
        ).scalars().all()
        assert rows == []
        await session.execute(delete(BrokerAccount).where(BrokerAccount.broker_id == broker_id))
        await session.commit()
