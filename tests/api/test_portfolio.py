"""Integration tests against a real Postgres instance for
GET /brokers/{broker_id}/portfolio. Reuses tests/api/test_trades.py's
fixtures (db_session/paper_broker_row/active_user/broker_grant/api_client)
rather than duplicating them - same rationale as that file's own docstring:
this endpoint needs the same DB/auth/broker-grant wiring proven there.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

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


# --- Selectable cost-basis method (D041) -------------------------------
#
# One shared multi-lot fill history, three methods, three different but
# individually hand-verified answers. Sequence (each trade fills at its
# estimated_price under the paper broker):
#   buy  10 AAPL @ 100  -> cash 100000 - 1000 =  99000
#   buy  10 AAPL @ 110  -> cash  99000 - 1100 =  97900
#   sell 15 AAPL @ 120  -> cash  97900 + 1800 =  99700
# leaving 5 shares open, valued at the supplied mark of 130 -> 650, so
# total_equity is 99700 + 650 = 100350 under every method (cash, quantity
# and current_value are method-independent - only the basis moves).
#
#   AVERAGE: basis (10*100 + 10*110)/20 = 105
#            realized (120-105)*15                    = 225
#            unrealized (130-105)*5                   = 125
#   FIFO:    sell eats 10 @ 100 then 5 @ 110; 5 @ 110 left -> basis 110
#            realized (120-100)*10 + (120-110)*5      = 250
#            unrealized (130-110)*5                   = 100
#   LIFO:    sell eats 10 @ 110 then 5 @ 100; 5 @ 100 left -> basis 100
#            realized (120-110)*10 + (120-100)*5      = 200
#            unrealized (130-100)*5                   = 150


async def _seed_multi_lot_history(client, broker_id, headers):
    """buy 10 @ 100, buy 10 @ 110, sell 15 @ 120 - the shared fixture the
    three cost-basis assertions below all read back."""
    for side, quantity, price, stop in (
        ("buy", "10", "100", "95"),
        ("buy", "10", "110", "104"),
        ("sell", "15", "120", "126"),
    ):
        response = await client.post(
            f"/brokers/{broker_id}/trades",
            headers=headers,
            json={
                "symbol": "AAPL",
                "side": side,
                "quantity": quantity,
                "estimated_price": price,
                "stop_price": stop,
            },
        )
        assert response.status_code in (200, 201), response.text


@pytest.mark.asyncio
async def test_each_cost_basis_method_reports_its_own_hand_verified_pnl():
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
            await _seed_multi_lot_history(client, broker_id, headers)

            bodies = {}
            for label, params in (
                ("default", None),
                ("average", {"cost_basis_method": "average"}),
                ("fifo", {"cost_basis_method": "fifo"}),
                ("lifo", {"cost_basis_method": "lifo"}),
            ):
                response = await client.request(
                    "GET",
                    f"/brokers/{broker_id}/portfolio",
                    headers=headers,
                    params=params,
                    json={"marks": {"AAPL": "130"}},
                )
                assert response.status_code == 200, (label, response.text)
                bodies[label] = response.json()

        expected = {
            "average": (Decimal("105"), Decimal("225"), Decimal("125")),
            "fifo": (Decimal("110"), Decimal("250"), Decimal("100")),
            "lifo": (Decimal("100"), Decimal("200"), Decimal("150")),
        }
        for label, (basis, realized, unrealized) in expected.items():
            body = bodies[label]
            assert len(body["positions"]) == 1, label
            position = body["positions"][0]
            assert position["symbol"] == "AAPL", label
            assert Decimal(position["quantity"]) == Decimal("5"), label
            assert Decimal(position["avg_cost"]) == basis, label
            assert Decimal(position["realized_pnl"]) == realized, label
            assert Decimal(position["unrealized_pnl"]) == unrealized, label
            assert Decimal(body["total_realized_pnl"]) == realized, label
            assert Decimal(body["total_unrealized_pnl"]) == unrealized, label
            # Method-independent figures must be identical across all three.
            assert Decimal(position["current_value"]) == Decimal("650"), label
            assert Decimal(body["cash"]) == Decimal("99700"), label
            assert Decimal(body["total_equity"]) == Decimal("100350"), label

        # Omitting the parameter entirely must be byte-identical to the old
        # endpoint - this is the backward-compatibility guarantee, not just
        # "numerically close".
        assert bodies["default"] == bodies["average"]

        # And the three must genuinely differ, or the parameter does nothing.
        realized_figures = {
            bodies[label]["total_realized_pnl"] for label in ("average", "fifo", "lifo")
        }
        assert len(realized_figures) == 3


@pytest.mark.asyncio
async def test_an_unrecognised_cost_basis_method_is_422_not_a_silent_default():
    """Fail closed: a typo'd method must be rejected outright rather than
    quietly answered with average-cost numbers the caller did not ask for."""
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
                params={"cost_basis_method": "hifo"},
            )
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Phase 36 (D044): the persisted write path now carries the cost-basis method
# too. The figures asserted below are the same hand-computed ones the read
# endpoint's tests above use (see the comment block preceding
# _seed_multi_lot_history) - deliberately, so a persisted FIFO snapshot is
# checked against FIFO's independently-derived answer rather than against
# whatever the code happened to store.
# ---------------------------------------------------------------------------

EXPECTED_BY_METHOD = {
    # method -> (avg_cost/basis, total_realized_pnl, total_unrealized_pnl)
    "average": (Decimal("105"), Decimal("225"), Decimal("125")),
    "fifo": (Decimal("110"), Decimal("250"), Decimal("100")),
    "lifo": (Decimal("100"), Decimal("200"), Decimal("150")),
}


def _assert_entry_matches(entry, method, label=""):
    """One POST response or history entry must both carry `method` and carry
    the numbers that method actually produces. Asserting the label alone
    would pass on a row that recorded `fifo` next to average-cost figures,
    which is the exact mislabelling this phase exists to prevent."""
    basis, realized, unrealized = EXPECTED_BY_METHOD[method]
    assert entry["cost_basis_method"] == method, (label, entry)
    assert Decimal(entry["positions"][0]["avg_cost"]) == basis, label
    assert Decimal(entry["total_realized_pnl"]) == realized, label
    assert Decimal(entry["total_unrealized_pnl"]) == unrealized, label
    # Method-independent figures are identical whatever the method.
    assert Decimal(entry["cash"]) == Decimal("99700"), label
    assert Decimal(entry["total_equity"]) == Decimal("100350"), label


@pytest.mark.asyncio
async def test_a_posted_snapshot_defaults_to_average_and_records_that_method():
    """A body with no cost_basis_method must persist exactly the row it
    persisted before D044 - average numbers - and must now say so rather
    than leaving the method implicit."""
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
            await _seed_multi_lot_history(client, broker_id, headers)

            posted = await client.post(
                f"/brokers/{broker_id}/portfolio/snapshots",
                headers=headers,
                json={"marks": {"AAPL": "130"}},
            )
            assert posted.status_code == 201, posted.text

            history = await client.get(
                f"/brokers/{broker_id}/portfolio/history", headers=headers
            )
            assert history.status_code == 200, history.text

        _assert_entry_matches(posted.json(), "average", "posted")
        entries = history.json()["snapshots"]
        assert len(entries) == 1
        _assert_entry_matches(entries[0], "average", "history")


@pytest.mark.asyncio
async def test_a_fifo_snapshot_persists_fifo_numbers_and_reads_back_as_fifo():
    """The gap D041 left open: FIFO on the write path, with the row saying
    which method produced it so GET .../history cannot be misread."""
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
            await _seed_multi_lot_history(client, broker_id, headers)

            posted = await client.post(
                f"/brokers/{broker_id}/portfolio/snapshots",
                headers=headers,
                json={"marks": {"AAPL": "130"}, "cost_basis_method": "fifo"},
            )
            assert posted.status_code == 201, posted.text

            history = await client.get(
                f"/brokers/{broker_id}/portfolio/history", headers=headers
            )
            assert history.status_code == 200, history.text

        _assert_entry_matches(posted.json(), "fifo", "posted")
        _assert_entry_matches(history.json()["snapshots"][0], "fifo", "history")


@pytest.mark.asyncio
async def test_history_distinguishes_snapshots_captured_under_different_methods():
    """The whole point of the column: three rows off one identical fill
    history, carrying three different realized-P&L figures, each labelled
    with the method that produced it. Without the label these would be an
    incoherent equity series."""
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
            await _seed_multi_lot_history(client, broker_id, headers)

            for method in ("average", "fifo", "lifo"):
                posted = await client.post(
                    f"/brokers/{broker_id}/portfolio/snapshots",
                    headers=headers,
                    json={"marks": {"AAPL": "130"}, "cost_basis_method": method},
                )
                assert posted.status_code == 201, (method, posted.text)

            history = await client.get(
                f"/brokers/{broker_id}/portfolio/history", headers=headers
            )
            assert history.status_code == 200, history.text

        entries = history.json()["snapshots"]
        assert len(entries) == 3
        by_method = {entry["cost_basis_method"]: entry for entry in entries}
        assert set(by_method) == {"average", "fifo", "lifo"}
        for method, entry in by_method.items():
            _assert_entry_matches(entry, method, method)

        # And they genuinely differ, or the field is decorative.
        assert len({entry["total_realized_pnl"] for entry in entries}) == 3


@pytest.mark.asyncio
async def test_a_cost_basis_method_in_the_posts_query_string_is_ignored_and_said_so():
    """D044 put the method in the POST *body*, not its query string (see the
    route module docstring). A caller copying the GET's `?cost_basis_method=`
    onto the POST gets average - which was already true pre-D044 and stays
    true - but the response now names the method used, so the mistake is
    visible instead of silently mislabelling the stored history."""
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
            await _seed_multi_lot_history(client, broker_id, headers)

            posted = await client.post(
                f"/brokers/{broker_id}/portfolio/snapshots",
                headers=headers,
                params={"cost_basis_method": "fifo"},
                json={"marks": {"AAPL": "130"}},
            )
            assert posted.status_code == 201, posted.text

        _assert_entry_matches(posted.json(), "average", "query-string ignored")


@pytest.mark.asyncio
async def test_an_unrecognised_cost_basis_method_in_the_post_body_is_422():
    """Fail closed on the write path too: a typo must never quietly persist
    a row of numbers the caller did not ask for."""
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
                f"/brokers/{broker_id}/portfolio/snapshots",
                headers={"Authorization": f"Bearer {token}"},
                json={"marks": {}, "cost_basis_method": "hifo"},
            )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_a_row_written_without_the_new_column_reads_back_as_average_never_null():
    """The migration's backward-compatibility guarantee (D044), exercised
    against the real column rather than asserted in prose.

    The INSERT below deliberately omits `cost_basis_method` entirely - which
    is precisely the shape of every row written before the migration - so
    the value under test is Postgres's own server_default, not anything the
    application supplied. It must read back as the string `average` and must
    be non-null: a null would surface as "method unknown", which is strictly
    less true than what we actually know about those rows (the write path
    was average-only until this phase).
    """
    snapshot_id = uuid.uuid4()
    async with (
        db_session() as session,
        active_user(session, permissions=(Permission.VIEW_PORTFOLIO.value,)) as (
            user_id,
            email,
        ),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        await session.execute(
            text(
                "INSERT INTO portfolio_snapshots "
                "(id, broker_id, cash, total_equity, total_unrealized_pnl, "
                " total_realized_pnl) "
                "VALUES (:id, :broker_id, 100000, 100000, 0, 0)"
            ),
            {"id": snapshot_id, "broker_id": broker_id},
        )
        await session.commit()

        stored = (
            await session.execute(
                text("SELECT cost_basis_method FROM portfolio_snapshots WHERE id = :id"),
                {"id": snapshot_id},
            )
        ).scalar_one()
        assert stored is not None
        assert stored == "average"

        async with api_client() as client:
            token = await get_token(client, email)
            history = await client.get(
                f"/brokers/{broker_id}/portfolio/history",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert history.status_code == 200, history.text
        entries = history.json()["snapshots"]
        assert len(entries) == 1
        assert entries[0]["cost_basis_method"] == "average"
