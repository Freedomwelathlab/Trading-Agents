"""Integration tests against a real Postgres instance for
GET /brokers/{broker_id}/portfolio. Reuses tests/api/test_trades.py's
fixtures (db_session/paper_broker_row/active_user/broker_grant/api_client)
rather than duplicating them - same rationale as that file's own docstring:
this endpoint needs the same DB/auth/broker-grant wiring proven there.
"""

import uuid
from decimal import Decimal

import pytest

from apps.api.app.auth.permissions import Permission
from tests.api.test_trades import _get_token as get_token
from tests.api.test_trades import (
    active_user,
    api_client,
    broker_grant,
    db_session,
    paper_broker_row,
)


@pytest.mark.asyncio
async def test_a_broker_with_no_trades_has_empty_positions_and_starting_cash():
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
            response = await client.request(
                "GET",
                f"/brokers/{broker_id}/portfolio",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["positions"] == []
        assert body["cash"] == "100000"
        assert body["total_equity"] == "100000"
        assert body["total_unrealized_pnl"] == "0"
        assert body["total_realized_pnl"] == "0"


@pytest.mark.asyncio
async def test_a_position_reports_avg_cost_and_unrealized_pnl_from_the_supplied_mark():
    async with (
        db_session() as session,
        active_user(
            session,
            permissions=(Permission.SUBMIT_PAPER_TRADE.value, Permission.VIEW_PORTFOLIO.value),
        ) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}

            await client.post(
                f"/brokers/{broker_id}/trades",
                headers=headers,
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )

            response = await client.request(
                "GET",
                f"/brokers/{broker_id}/portfolio",
                headers=headers,
                json={"marks": {"AAPL": "120"}},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["positions"]) == 1
        position = body["positions"][0]
        assert position["symbol"] == "AAPL"
        assert Decimal(position["quantity"]) == Decimal("10")
        assert Decimal(position["avg_cost"]) == Decimal("100")
        assert Decimal(position["current_value"]) == Decimal("1200")
        assert Decimal(position["unrealized_pnl"]) == Decimal("200")
        assert Decimal(position["realized_pnl"]) == Decimal("0")
        assert Decimal(body["cash"]) == Decimal("99000")
        assert Decimal(body["total_equity"]) == Decimal("100200")
        assert Decimal(body["total_unrealized_pnl"]) == Decimal("200")


@pytest.mark.asyncio
async def test_a_missing_mark_for_a_held_position_is_400_data_unavailable():
    async with (
        db_session() as session,
        active_user(
            session,
            permissions=(Permission.SUBMIT_PAPER_TRADE.value, Permission.VIEW_PORTFOLIO.value),
        ) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}

            await client.post(
                f"/brokers/{broker_id}/trades",
                headers=headers,
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )

            response = await client.request(
                "GET",
                f"/brokers/{broker_id}/portfolio",
                headers=headers,
            )

        assert response.status_code == 400
        assert "DATA_UNAVAILABLE" in response.json()["detail"]


@pytest.mark.asyncio
async def test_realized_pnl_survives_a_full_close_of_the_position():
    async with (
        db_session() as session,
        active_user(
            session,
            permissions=(Permission.SUBMIT_PAPER_TRADE.value, Permission.VIEW_PORTFOLIO.value),
        ) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}

            await client.post(
                f"/brokers/{broker_id}/trades",
                headers=headers,
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )
            await client.post(
                f"/brokers/{broker_id}/trades",
                headers=headers,
                json={
                    "symbol": "AAPL",
                    "side": "sell",
                    "quantity": "10",
                    "estimated_price": "150",
                    "stop_price": "155",
                },
            )

            response = await client.request(
                "GET",
                f"/brokers/{broker_id}/portfolio",
                headers=headers,
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["positions"] == []
        assert Decimal(body["total_realized_pnl"]) == Decimal("500")
        assert Decimal(body["total_unrealized_pnl"]) == Decimal("0")
        assert Decimal(body["cash"]) == Decimal("100500")


@pytest.mark.asyncio
async def test_view_portfolio_permission_alone_cannot_submit_a_trade():
    """Confirms VIEW_PORTFOLIO is genuinely weaker than SUBMIT_PAPER_TRADE
    (D022's stated reason for keeping them separate), not an accidental
    superset."""
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
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_a_user_without_view_portfolio_permission_gets_403():
    async with (
        db_session() as session,
        active_user(session, permissions=()) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            response = await client.request(
                "GET",
                f"/brokers/{broker_id}/portfolio",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_no_broker_grant_gets_403_even_with_the_permission():
    async with (
        db_session() as session,
        active_user(session, permissions=(Permission.VIEW_PORTFOLIO.value,)) as (
            user_id,
            email,
        ),
        paper_broker_row(session) as broker_id,
        # deliberately no broker_grant() here
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            response = await client.request(
                "GET",
                f"/brokers/{broker_id}/portfolio",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_unknown_broker_returns_404():
    async with db_session() as session, active_user(
        session, permissions=(Permission.VIEW_PORTFOLIO.value,)
    ) as (_user_id, email):
        async with api_client() as client:
            token = await get_token(client, email)
            response = await client.request(
                "GET",
                f"/brokers/{uuid.uuid4()}/portfolio",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_request_with_no_token_is_rejected():
    async with db_session() as session, paper_broker_row(session) as broker_id:
        async with api_client() as client:
            response = await client.request("GET", f"/brokers/{broker_id}/portfolio")
    assert response.status_code == 401


# --- Persisted snapshot history (D027) ---------------------------------


@pytest.mark.asyncio
async def test_posting_a_snapshot_persists_it_and_it_appears_in_history():
    async with (
        db_session() as session,
        active_user(
            session,
            permissions=(Permission.SUBMIT_PAPER_TRADE.value, Permission.VIEW_PORTFOLIO.value),
        ) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}

            await client.post(
                f"/brokers/{broker_id}/trades",
                headers=headers,
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )

            post_response = await client.post(
                f"/brokers/{broker_id}/portfolio/snapshots",
                headers=headers,
                json={"marks": {"AAPL": "120"}},
            )
            assert post_response.status_code == 201, post_response.text
            posted = post_response.json()
            assert posted["broker_id"] == str(broker_id)
            assert len(posted["positions"]) == 1
            assert posted["positions"][0]["symbol"] == "AAPL"
            assert Decimal(posted["positions"][0]["current_value"]) == Decimal("1200")
            assert Decimal(posted["total_equity"]) == Decimal("100200")

            history_response = await client.get(
                f"/brokers/{broker_id}/portfolio/history",
                headers=headers,
            )

        assert history_response.status_code == 200, history_response.text
        history = history_response.json()
        assert len(history["snapshots"]) == 1
        entry = history["snapshots"][0]
        assert entry["id"] == posted["id"]
        assert entry["captured_at"] == posted["captured_at"]
        assert Decimal(entry["cash"]) == Decimal("99000")
        assert Decimal(entry["total_unrealized_pnl"]) == Decimal("200")
        assert history["limit"] == 50
        assert history["offset"] == 0


@pytest.mark.asyncio
async def test_history_is_ordered_oldest_to_newest_and_paginates():
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

            posted_ids = []
            for _ in range(3):
                response = await client.post(
                    f"/brokers/{broker_id}/portfolio/snapshots",
                    headers=headers,
                    json={"marks": {}},
                )
                assert response.status_code == 201, response.text
                posted_ids.append(response.json()["id"])

            full_history = await client.get(
                f"/brokers/{broker_id}/portfolio/history",
                headers=headers,
            )
            assert full_history.status_code == 200
            full_ids = [s["id"] for s in full_history.json()["snapshots"]]
            assert full_ids == posted_ids

            page_1 = await client.get(
                f"/brokers/{broker_id}/portfolio/history",
                headers=headers,
                params={"limit": 2, "offset": 0},
            )
            page_2 = await client.get(
                f"/brokers/{broker_id}/portfolio/history",
                headers=headers,
                params={"limit": 2, "offset": 2},
            )

        assert [s["id"] for s in page_1.json()["snapshots"]] == posted_ids[:2]
        assert page_1.json()["limit"] == 2
        assert [s["id"] for s in page_2.json()["snapshots"]] == posted_ids[2:]


@pytest.mark.asyncio
async def test_posting_a_snapshot_with_a_missing_mark_is_400_and_nothing_is_persisted():
    async with (
        db_session() as session,
        active_user(
            session,
            permissions=(Permission.SUBMIT_PAPER_TRADE.value, Permission.VIEW_PORTFOLIO.value),
        ) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}

            await client.post(
                f"/brokers/{broker_id}/trades",
                headers=headers,
                json={
                    "symbol": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "estimated_price": "100",
                    "stop_price": "95",
                },
            )

            post_response = await client.post(
                f"/brokers/{broker_id}/portfolio/snapshots",
                headers=headers,
                json={"marks": {}},
            )
            assert post_response.status_code == 400
            assert "DATA_UNAVAILABLE" in post_response.json()["detail"]

            history_response = await client.get(
                f"/brokers/{broker_id}/portfolio/history",
                headers=headers,
            )

        assert history_response.status_code == 200
        assert history_response.json()["snapshots"] == []


@pytest.mark.asyncio
async def test_snapshot_post_requires_view_portfolio_permission():
    async with (
        db_session() as session,
        active_user(session, permissions=()) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            response = await client.post(
                f"/brokers/{broker_id}/portfolio/snapshots",
                headers={"Authorization": f"Bearer {token}"},
                json={"marks": {}},
            )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_history_requires_view_portfolio_permission():
    async with (
        db_session() as session,
        active_user(session, permissions=()) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            response = await client.get(
                f"/brokers/{broker_id}/portfolio/history",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_snapshot_post_without_broker_grant_gets_403():
    async with (
        db_session() as session,
        active_user(session, permissions=(Permission.VIEW_PORTFOLIO.value,)) as (
            user_id,
            email,
        ),
        paper_broker_row(session) as broker_id,
        # deliberately no broker_grant() here
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            response = await client.post(
                f"/brokers/{broker_id}/portfolio/snapshots",
                headers={"Authorization": f"Bearer {token}"},
                json={"marks": {}},
            )
    assert response.status_code == 403
