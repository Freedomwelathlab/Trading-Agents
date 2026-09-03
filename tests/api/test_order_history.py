"""Integration tests against a real Postgres instance for the Phase 48
order/fill history routes (docs/DECISIONS.md D065):

  GET /brokers/{broker_id}/orders
  GET /brokers/{broker_id}/orders/{order_id}
  GET /brokers/{broker_id}/fills

Reuses tests/api/test_trades.py's fixtures
(db_session/paper_broker_row/active_user/broker_grant/api_client) rather
than duplicating them - same rationale as tests/api/test_portfolio.py:
these endpoints need the same DB/auth/broker-grant wiring proven there,
and `paper_broker_row`'s teardown already knows how to remove the
orders/fills rows these tests create.

Every order these tests read back was created by a real POST through the
real trade path - never by an INSERT - so what is asserted is what the
OMS actually persists, not what a fixture author thought it persists.
"""

import uuid
from decimal import Decimal

import pytest

from apps.api.app.auth.permissions import Permission
from apps.api.app.db.models import BrokerKind, OrderStatus
from apps.api.app.db.models import Order as OrderRow
from apps.api.app.risk.models import Side
from tests.api.test_trades import _get_token as get_token
from tests.api.test_trades import (
    active_user,
    api_client,
    broker_grant,
    db_session,
    paper_broker_row,
)

READ_AND_TRADE = (Permission.SUBMIT_PAPER_TRADE.value, Permission.VIEW_PORTFOLIO.value)


DEFAULT_MARKS = {"AAPL": "100", "MSFT": "100", "NVDA": "100", "TSLA": "100"}
"""Marks for every symbol these tests ever hold. A second trade on a
broker that already holds a position needs a mark for that position or
the request is a 400 DATA_UNAVAILABLE (spec Sec57, and correctly so) -
supplying the whole set keeps that requirement from turning every
multi-order test into a bookkeeping exercise. A mark for a symbol not
actually held is ignored."""


async def _submit(client, broker_id, headers, **overrides):
    """One real paper trade through the real endpoint."""
    payload = {
        "symbol": "AAPL",
        "side": "buy",
        "quantity": "10",
        "estimated_price": "100",
        "stop_price": "95",
        "marks": dict(DEFAULT_MARKS),
    }
    payload.update(overrides)
    response = await client.post(
        f"/brokers/{broker_id}/trades", headers=headers, json=payload
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- Empty state -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_broker_with_no_orders_returns_an_empty_page_not_an_error():
    """An empty audit trail is a real answer, not a 404 - same posture as
    D027's history endpoint."""
    async with (
        db_session() as session,
        active_user(session, permissions=(Permission.VIEW_PORTFOLIO.value,)) as (
            user_id,
            email,
        ),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            orders = await client.get(f"/brokers/{broker_id}/orders", headers=headers)
            fills = await client.get(f"/brokers/{broker_id}/fills", headers=headers)

    assert orders.status_code == 200, orders.text
    assert orders.json() == {"orders": [], "limit": 50, "offset": 0}
    assert fills.status_code == 200, fills.text
    assert fills.json() == {"fills": [], "limit": 50, "offset": 0}


# --- Populated listing -------------------------------------------------


@pytest.mark.asyncio
async def test_a_filled_order_reads_back_with_every_persisted_field_and_its_fill():
    async with (
        db_session() as session,
        active_user(session, permissions=READ_AND_TRADE) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            submitted = await _submit(client, broker_id, headers)
            listing = await client.get(f"/brokers/{broker_id}/orders", headers=headers)

        assert listing.status_code == 200, listing.text
        body = listing.json()
        assert len(body["orders"]) == 1
        order = body["orders"][0]

        assert order["id"] == submitted["order_id"]
        assert order["broker_id"] == str(broker_id)
        assert order["broker_kind"] == "paper"
        assert order["symbol"] == "AAPL"
        assert order["side"] == "buy"
        assert Decimal(order["quantity"]) == Decimal("10")
        assert Decimal(order["estimated_price"]) == Decimal("100")
        assert Decimal(order["stop_price"]) == Decimal("95")
        assert order["status"] == "filled"
        assert order["risk_block_reason"] is None
        assert order["portfolio_action"] == "approve"
        assert order["submitted_by_user_id"] == str(user_id)
        assert order["submitted_at"] is not None

        # The fill the paper broker actually produced - one, full.
        assert len(order["fills"]) == 1
        fill = order["fills"][0]
        assert fill["order_id"] == order["id"]
        assert Decimal(fill["quantity"]) == Decimal("10")
        assert Decimal(fill["fill_price"]) == Decimal("100")
        assert fill["filled_at"] is not None


@pytest.mark.asyncio
async def test_orders_are_listed_newest_first():
    """A blotter reads newest-first, unlike D027's oldest-first equity
    curve - so this is asserted rather than assumed."""
    async with (
        db_session() as session,
        active_user(session, permissions=READ_AND_TRADE) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            submitted = [
                (await _submit(client, broker_id, headers, symbol=symbol))["order_id"]
                for symbol in ("AAPL", "MSFT", "NVDA")
            ]
            listing = await client.get(f"/brokers/{broker_id}/orders", headers=headers)

        assert listing.status_code == 200, listing.text
        returned = [order["id"] for order in listing.json()["orders"]]
        assert returned == list(reversed(submitted))


@pytest.mark.asyncio
async def test_a_rejected_order_is_listed_with_its_block_reason_and_no_fills():
    """The rejections are the part of the trail that shows the controls
    working; the listing must not quietly be a record of successes only.
    `quantity=0` is refused by request validation, so this uses a size the
    deterministic risk engine actually blocks."""
    async with (
        db_session() as session,
        active_user(session, permissions=READ_AND_TRADE) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}

            response = await client.post(
                f"/brokers/{broker_id}/trades",
                headers=headers,
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "100000",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "rejected", response.text

            listing = await client.get(f"/brokers/{broker_id}/orders", headers=headers)
            fills = await client.get(f"/brokers/{broker_id}/fills", headers=headers)

        assert listing.status_code == 200, listing.text
        orders = listing.json()["orders"]
        assert len(orders) == 1
        assert orders[0]["status"] == "rejected"
        assert orders[0]["fills"] == []
        # SOMETHING must say why - either the Risk Engine or the Portfolio
        # Manager. Which one is D029's business, not this endpoint's; what
        # this endpoint owes the reader is that the reason is present.
        assert (
            orders[0]["risk_block_reason"] is not None
            or orders[0]["portfolio_action"] == "reject"
        )

        # ...and a rejected order contributes no row to the blotter.
        assert fills.json()["fills"] == []


@pytest.mark.asyncio
async def test_both_of_d029s_quantities_are_surfaced_and_agree_on_an_approved_order():
    """D029's resize has to stay readable in the history: `quantity` is
    what was acted on, `portfolio_requested_quantity` what the Risk Engine
    had approved before the Portfolio Manager saw it, and the two differ
    exactly when `portfolio_action` is `modify`. Under the committed
    default limits a single trade on a fresh account cannot reach a
    resize - `risk_max_position_pct_of_equity` (10%) binds well before
    `portfolio_max_symbol_pct_of_equity` (25%) - so what is asserted here
    is the honest, reachable half: both fields are present, and on an
    APPROVE they agree. A test that fabricated a resize by rewriting the
    limits would be testing the Portfolio Manager, which this phase does
    not touch."""
    async with (
        db_session() as session,
        active_user(session, permissions=READ_AND_TRADE) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            await _submit(client, broker_id, headers)
            listing = await client.get(f"/brokers/{broker_id}/orders", headers=headers)

        order = listing.json()["orders"][0]
        assert order["portfolio_action"] == "approve"
        assert order["portfolio_binding_constraint"] is None
        assert Decimal(order["portfolio_requested_quantity"]) == Decimal("10")
        assert Decimal(order["quantity"]) == Decimal("10")


# --- Pagination --------------------------------------------------------


@pytest.mark.asyncio
async def test_orders_pagination_boundary_reassembles_into_the_full_page():
    """Two single-row pages, in order, must equal the two-row page - the
    same property D031's admin listings assert. Ordering is total
    (`submitted_at DESC, id DESC`), so this cannot pass by luck."""
    async with (
        db_session() as session,
        active_user(session, permissions=READ_AND_TRADE) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            for symbol in ("AAPL", "MSFT", "NVDA"):
                await _submit(client, broker_id, headers, symbol=symbol)

            everything = await client.get(f"/brokers/{broker_id}/orders", headers=headers)
            page_1 = await client.get(
                f"/brokers/{broker_id}/orders", headers=headers, params={"limit": 2, "offset": 0}
            )
            page_2 = await client.get(
                f"/brokers/{broker_id}/orders", headers=headers, params={"limit": 2, "offset": 2}
            )
            past_the_end = await client.get(
                f"/brokers/{broker_id}/orders", headers=headers, params={"limit": 2, "offset": 99}
            )

        all_ids = [o["id"] for o in everything.json()["orders"]]
        assert len(all_ids) == 3
        assert [o["id"] for o in page_1.json()["orders"]] == all_ids[:2]
        assert page_1.json()["limit"] == 2
        assert page_1.json()["offset"] == 0
        assert [o["id"] for o in page_2.json()["orders"]] == all_ids[2:]
        assert page_2.json()["offset"] == 2
        # Past the end is an empty page, not an error.
        assert past_the_end.status_code == 200
        assert past_the_end.json()["orders"] == []


@pytest.mark.asyncio
async def test_out_of_range_pagination_arguments_are_422_on_all_three_routes():
    """`limit=501`/`limit=0`/`offset=-1` are refused rather than silently
    clamped - same as D031/D034."""
    async with (
        db_session() as session,
        active_user(session, permissions=(Permission.VIEW_PORTFOLIO.value,)) as (
            user_id,
            email,
        ),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            responses = []
            for path in ("orders", "fills"):
                for params in ({"limit": 501}, {"limit": 0}, {"offset": -1}):
                    responses.append(
                        await client.get(
                            f"/brokers/{broker_id}/{path}", headers=headers, params=params
                        )
                    )
            # The documented ceiling itself is accepted, so the boundary is
            # exactly 500 and not something smaller by accident.
            at_ceiling = await client.get(
                f"/brokers/{broker_id}/orders", headers=headers, params={"limit": 500}
            )

    assert [r.status_code for r in responses] == [422] * 6
    assert at_ceiling.status_code == 200
    assert at_ceiling.json()["limit"] == 500


# --- Single-order detail -----------------------------------------------


@pytest.mark.asyncio
async def test_order_detail_matches_the_listing_row_byte_for_byte():
    """Both routes render through the same `_to_order_response`, and this
    asserts that rather than trusting it: an order read one way must never
    disagree with the same order read the other."""
    async with (
        db_session() as session,
        active_user(session, permissions=READ_AND_TRADE) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            submitted = await _submit(client, broker_id, headers)
            order_id = submitted["order_id"]

            listing = await client.get(f"/brokers/{broker_id}/orders", headers=headers)
            detail = await client.get(
                f"/brokers/{broker_id}/orders/{order_id}", headers=headers
            )

    assert detail.status_code == 200, detail.text
    assert detail.json() == listing.json()["orders"][0]


@pytest.mark.asyncio
async def test_order_detail_carries_zero_fills_for_a_rejected_order():
    async with (
        db_session() as session,
        active_user(session, permissions=READ_AND_TRADE) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            rejected = await client.post(
                f"/brokers/{broker_id}/trades",
                headers=headers,
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "100000",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )
            assert rejected.json()["status"] == "rejected", rejected.text
            detail = await client.get(
                f"/brokers/{broker_id}/orders/{rejected.json()['order_id']}",
                headers=headers,
            )

    assert detail.status_code == 200, detail.text
    assert detail.json()["fills"] == []
    assert detail.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_a_many_order_broker_maps_each_order_to_only_its_own_fill():
    """The listing loads fills for a whole page in one query and groups
    them by order_id; this is the assertion that the grouping is right and
    not, say, every order carrying every fill."""
    async with (
        db_session() as session,
        active_user(session, permissions=READ_AND_TRADE) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            prices = {"AAPL": "100", "MSFT": "200", "NVDA": "300"}
            for symbol, price in prices.items():
                await _submit(
                    client,
                    broker_id,
                    headers,
                    symbol=symbol,
                    estimated_price=price,
                    stop_price=str(Decimal(price) * Decimal("0.95")),
                    marks=prices,
                )
            listing = await client.get(f"/brokers/{broker_id}/orders", headers=headers)

        orders = listing.json()["orders"]
        assert len(orders) == 3
        for order in orders:
            assert len(order["fills"]) == 1, order["symbol"]
            assert order["fills"][0]["order_id"] == order["id"]
            # The fill price is the order's own price, not another order's.
            assert Decimal(order["fills"][0]["fill_price"]) == Decimal(
                order["estimated_price"]
            )


@pytest.mark.asyncio
async def test_an_unknown_order_id_is_404():
    async with (
        db_session() as session,
        active_user(session, permissions=(Permission.VIEW_PORTFOLIO.value,)) as (
            user_id,
            email,
        ),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            response = await client.get(
                f"/brokers/{broker_id}/orders/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_real_order_id_from_another_broker_is_404_not_a_leak():
    """The caller holds a grant on broker A and asks A for an order that
    really exists on broker B. Answering anything other than 404 would make
    the route an existence oracle for every other broker's order ids."""
    async with (
        db_session() as session,
        active_user(session, permissions=READ_AND_TRADE) as (user_id, email),
        paper_broker_row(session) as broker_a,
        paper_broker_row(session) as broker_b,
        broker_grant(session, user_id=user_id, broker_id=broker_a),
        broker_grant(session, user_id=user_id, broker_id=broker_b),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            on_b = await _submit(client, broker_b, headers)

            cross = await client.get(
                f"/brokers/{broker_a}/orders/{on_b['order_id']}", headers=headers
            )
            # ...and it is genuinely readable on its own broker, so the 404
            # above is about scoping and not about a missing row.
            direct = await client.get(
                f"/brokers/{broker_b}/orders/{on_b['order_id']}", headers=headers
            )

    assert cross.status_code == 404
    assert direct.status_code == 200, direct.text


@pytest.mark.asyncio
async def test_one_brokers_listing_never_contains_another_brokers_orders():
    async with (
        db_session() as session,
        active_user(session, permissions=READ_AND_TRADE) as (user_id, email),
        paper_broker_row(session) as broker_a,
        paper_broker_row(session) as broker_b,
        broker_grant(session, user_id=user_id, broker_id=broker_a),
        broker_grant(session, user_id=user_id, broker_id=broker_b),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            on_a = await _submit(client, broker_a, headers, symbol="AAPL")
            on_b = await _submit(client, broker_b, headers, symbol="MSFT")

            listing_a = await client.get(f"/brokers/{broker_a}/orders", headers=headers)
            fills_a = await client.get(f"/brokers/{broker_a}/fills", headers=headers)

        assert [o["id"] for o in listing_a.json()["orders"]] == [on_a["order_id"]]
        assert [f["order_id"] for f in fills_a.json()["fills"]] == [on_a["order_id"]]
        assert on_b["order_id"] not in [o["id"] for o in listing_a.json()["orders"]]


# --- Fill blotter ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_fill_blotter_is_one_row_per_execution_newest_first():
    async with (
        db_session() as session,
        active_user(session, permissions=READ_AND_TRADE) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            submitted = [
                (await _submit(client, broker_id, headers, symbol=symbol))["order_id"]
                for symbol in ("AAPL", "MSFT", "NVDA")
            ]
            # ...plus one rejected order, which must contribute no row.
            rejected = await client.post(
                f"/brokers/{broker_id}/trades",
                headers=headers,
                json={
                    "symbol": "TSLA",
                    "side": "buy",
                    "quantity": "100000",
                    "estimated_price": "100",
                    "stop_price": "95",
                    "marks": dict(DEFAULT_MARKS),
                },
            )
            assert rejected.status_code == 200, rejected.text
            assert rejected.json()["status"] == "rejected", rejected.text

            blotter = await client.get(f"/brokers/{broker_id}/fills", headers=headers)
            page_1 = await client.get(
                f"/brokers/{broker_id}/fills", headers=headers, params={"limit": 2, "offset": 0}
            )
            page_2 = await client.get(
                f"/brokers/{broker_id}/fills", headers=headers, params={"limit": 2, "offset": 2}
            )

        assert blotter.status_code == 200, blotter.text
        rows = blotter.json()["fills"]
        assert len(rows) == 3
        assert [r["order_id"] for r in rows] == list(reversed(submitted))
        assert [r["symbol"] for r in rows] == ["NVDA", "MSFT", "AAPL"]
        assert all(r["side"] == "buy" for r in rows)
        assert all(r["broker_id"] == str(broker_id) for r in rows)
        assert all(r["broker_kind"] == "paper" for r in rows)
        assert all(Decimal(r["fill_price"]) == Decimal("100") for r in rows)
        assert "TSLA" not in [r["symbol"] for r in rows]

        assert [r["id"] for r in page_1.json()["fills"]] == [r["id"] for r in rows[:2]]
        assert [r["id"] for r in page_2.json()["fills"]] == [r["id"] for r in rows[2:]]


# --- Paper/live labelling ----------------------------------------------


@pytest.mark.asyncio
async def test_a_live_kind_brokers_orders_are_labelled_live():
    """`orders` has no paper/live column - `broker_kind` is the joined
    `brokers.kind` (D065). This proves the join is real by reading back an
    order recorded against a LIVE-kind broker row.

    The order itself is INSERTed directly here rather than submitted: a
    live trade would require an actually-configured live execution path
    (D058), and nothing in this phase may enable one. What is under test is
    the response labelling, not the live path, and the row inserted is
    exactly the shape the OMS writes.
    """
    order_id = uuid.uuid4()
    async with (
        db_session() as session,
        active_user(session, permissions=(Permission.VIEW_PORTFOLIO.value,)) as (
            user_id,
            email,
        ),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        session.add(
            OrderRow(
                id=order_id,
                broker_id=broker_id,
                symbol="AAPL",
                side=Side.BUY,
                quantity=Decimal("10"),
                estimated_price=Decimal("100"),
                stop_price=Decimal("95"),
                status=OrderStatus.FILLED,
                submitted_by_user_id=user_id,
            )
        )
        await session.commit()

        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            listing = await client.get(f"/brokers/{broker_id}/orders", headers=headers)
            detail = await client.get(
                f"/brokers/{broker_id}/orders/{order_id}", headers=headers
            )

    assert listing.status_code == 200, listing.text
    assert listing.json()["orders"][0]["broker_kind"] == "live"
    assert detail.status_code == 200, detail.text
    assert detail.json()["broker_kind"] == "live"


# --- Authorization -----------------------------------------------------


@pytest.mark.asyncio
async def test_all_three_routes_need_the_view_portfolio_permission():
    async with (
        db_session() as session,
        active_user(session, permissions=()) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            responses = [
                await client.get(f"/brokers/{broker_id}/orders", headers=headers),
                await client.get(
                    f"/brokers/{broker_id}/orders/{uuid.uuid4()}", headers=headers
                ),
                await client.get(f"/brokers/{broker_id}/fills", headers=headers),
            ]
    assert [r.status_code for r in responses] == [403, 403, 403]


@pytest.mark.asyncio
async def test_the_permission_alone_is_not_enough_without_a_broker_grant():
    """Cross-broker denial: the caller holds `portfolio:view` and the
    broker exists, but there is no BrokerGrant - 403, exactly as
    require_broker_access answers for every other broker-scoped route."""
    async with (
        db_session() as session,
        active_user(session, permissions=(Permission.VIEW_PORTFOLIO.value,)) as (
            _user_id,
            email,
        ),
        paper_broker_row(session) as broker_id,
        # deliberately no broker_grant() here
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            responses = [
                await client.get(f"/brokers/{broker_id}/orders", headers=headers),
                await client.get(
                    f"/brokers/{broker_id}/orders/{uuid.uuid4()}", headers=headers
                ),
                await client.get(f"/brokers/{broker_id}/fills", headers=headers),
            ]
    assert [r.status_code for r in responses] == [403, 403, 403]


@pytest.mark.asyncio
async def test_a_second_users_grant_does_not_grant_this_user_anything():
    """The grant is per (user, broker), not per broker: a broker somebody
    else can read stays 403 here even though real orders exist on it."""
    async with (
        db_session() as session,
        active_user(session, permissions=READ_AND_TRADE) as (owner_id, owner_email),
        active_user(session, permissions=READ_AND_TRADE) as (_other_id, other_email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=owner_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            owner_headers = {
                "Authorization": f"Bearer {await get_token(client, owner_email)}"
            }
            await _submit(client, broker_id, owner_headers)

            owner_view = await client.get(
                f"/brokers/{broker_id}/orders", headers=owner_headers
            )
            other_headers = {
                "Authorization": f"Bearer {await get_token(client, other_email)}"
            }
            other_view = await client.get(
                f"/brokers/{broker_id}/orders", headers=other_headers
            )
            other_fills = await client.get(
                f"/brokers/{broker_id}/fills", headers=other_headers
            )

    assert owner_view.status_code == 200
    assert len(owner_view.json()["orders"]) == 1
    assert other_view.status_code == 403
    assert other_fills.status_code == 403


@pytest.mark.asyncio
async def test_an_unknown_broker_is_404_before_anything_else():
    async with db_session() as session, active_user(
        session, permissions=(Permission.VIEW_PORTFOLIO.value,)
    ) as (_user_id, email):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            missing = uuid.uuid4()
            responses = [
                await client.get(f"/brokers/{missing}/orders", headers=headers),
                await client.get(f"/brokers/{missing}/orders/{uuid.uuid4()}", headers=headers),
                await client.get(f"/brokers/{missing}/fills", headers=headers),
            ]
    assert [r.status_code for r in responses] == [404, 404, 404]


@pytest.mark.asyncio
async def test_no_token_is_401_on_all_three_routes():
    async with db_session() as session, paper_broker_row(session) as broker_id:
        async with api_client() as client:
            responses = [
                await client.get(f"/brokers/{broker_id}/orders"),
                await client.get(f"/brokers/{broker_id}/orders/{uuid.uuid4()}"),
                await client.get(f"/brokers/{broker_id}/fills"),
            ]
    assert [r.status_code for r in responses] == [401, 401, 401]


@pytest.mark.asyncio
async def test_these_routes_are_read_only_and_reject_writes():
    """Nothing in this phase may create or change an order. A POST/DELETE
    against these paths must be a 405, not an accidental second write
    path."""
    async with (
        db_session() as session,
        active_user(session, permissions=READ_AND_TRADE) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            submitted = await _submit(client, broker_id, headers)
            responses = [
                await client.post(f"/brokers/{broker_id}/orders", headers=headers, json={}),
                await client.post(f"/brokers/{broker_id}/fills", headers=headers, json={}),
                await client.delete(
                    f"/brokers/{broker_id}/orders/{submitted['order_id']}", headers=headers
                ),
            ]

            # ...and the order is still exactly there afterwards.
            still_there = await client.get(f"/brokers/{broker_id}/orders", headers=headers)

    assert [r.status_code for r in responses] == [405, 405, 405]
    assert len(still_there.json()["orders"]) == 1
