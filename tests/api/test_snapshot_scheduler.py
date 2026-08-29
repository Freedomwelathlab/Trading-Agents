"""Integration tests against a real Postgres instance for Phase 27's
automatic portfolio snapshotting (docs/DECISIONS.md D030).

Reuses tests/api/test_trades.py's fixtures (db_session/paper_broker_row/
active_user/broker_grant/api_client) for the same reason
tests/api/test_portfolio.py does - the scheduler reads exactly the
broker_accounts/broker_positions/orders/fills state those fixtures build
through the real trade path, and writes to the same portfolio_snapshots
tables that fixture already cleans up.

The only test double anywhere in this file is at the market-data vendor
boundary (FakeSymbolProvider, from the unit tests). The MarketDataRouter,
the scheduler, compute_portfolio_snapshot(), the persistence layer, and
Postgres are all the real thing.
"""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from apps.api.app.auth.permissions import Permission
from apps.api.app.db.base import get_session_factory
from apps.api.app.db.models import PortfolioSnapshotRow
from apps.api.app.marketdata.provider import DataUnavailableError
from apps.api.app.marketdata.router import MarketDataRouter
from apps.api.app.portfolio.scheduler import (
    PortfolioSnapshotScheduler,
    ScheduledSnapshotStatus,
    capture_scheduled_snapshot,
    eligible_broker_ids,
    open_position_symbols,
    run_snapshot_cycle,
)
from tests.api.test_trades import _get_token as get_token
from tests.api.test_trades import (
    active_user,
    api_client,
    broker_grant,
    db_session,
    paper_broker_row,
)
from tests.portfolio.test_scheduler import FakeSymbolProvider


async def _buy(
    client, token, broker_id, *, symbol="AAPL", quantity="10", price="100", stop="95", marks=None
):
    """Puts a real filled position on the broker through the real trade
    endpoint - risk engine, OMS, paper broker, and persistence included. No
    position row in these tests is inserted by hand.

    `marks` is the trade endpoint's own requirement, not the scheduler's:
    the risk engine must value every *already-held* position to check
    exposure, so a second buy while holding something else has to supply
    that other symbol's mark. It carries the same no-fabrication discipline
    the scheduler does, which is why this argument exists at all.
    """
    body = {
        "symbol": symbol,
        "side": "buy",
        "quantity": quantity,
        "estimated_price": price,
        "stop_price": stop,
    }
    if marks is not None:
        body["marks"] = marks
    response = await client.post(
        f"/brokers/{broker_id}/trades",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "filled", response.text


async def _snapshot_rows(session, broker_id):
    return (
        (
            await session.execute(
                select(PortfolioSnapshotRow)
                .where(PortfolioSnapshotRow.broker_id == broker_id)
                .options(selectinload(PortfolioSnapshotRow.positions))
                .order_by(PortfolioSnapshotRow.captured_at.asc())
            )
        )
        .scalars()
        .all()
    )


ALL_PERMS = (Permission.SUBMIT_PAPER_TRADE.value, Permission.VIEW_PORTFOLIO.value)


@pytest.mark.asyncio
async def test_a_broker_with_no_open_positions_is_captured_without_any_market_data():
    """A cash-only account needs no marks, so it is a complete valuation
    even with market data entirely NOT_CONFIGURED - not a degraded one."""
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        # Buy then sell the whole position, so the broker_accounts row
        # exists (making it eligible) with no open position left.
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, broker_id)
            sold = await client.post(
                f"/brokers/{broker_id}/trades",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "symbol": "AAPL",
                    "side": "sell",
                    "quantity": "10",
                    "estimated_price": "110",
                    "stop_price": "115",
                },
            )
            assert sold.status_code == 200, sold.text
            assert sold.json()["status"] == "filled", sold.text

        assert await open_position_symbols(session, broker_id) == []

        outcome = await capture_scheduled_snapshot(broker_id, session, market_data_router=None)

        assert outcome.status is ScheduledSnapshotStatus.CAPTURED
        assert outcome.snapshot_id is not None

        rows = await _snapshot_rows(session, broker_id)
        assert len(rows) == 1
        assert rows[0].positions == []
        # 100000 - 10*100 + 10*110 = 100100, all real fills, no marks needed.
        assert rows[0].cash == Decimal("100100")
        assert rows[0].total_equity == Decimal("100100")
        assert rows[0].total_realized_pnl == Decimal("100")


@pytest.mark.asyncio
async def test_a_held_position_is_valued_from_a_real_router_quote_and_persisted():
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, broker_id)

        assert await open_position_symbols(session, broker_id) == ["AAPL"]

        router = MarketDataRouter([FakeSymbolProvider({"AAPL": Decimal("120")})])
        outcome = await capture_scheduled_snapshot(broker_id, session, market_data_router=router)

        assert outcome.status is ScheduledSnapshotStatus.CAPTURED

        rows = await _snapshot_rows(session, broker_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.id == outcome.snapshot_id
        assert row.cash == Decimal("99000")
        assert len(row.positions) == 1
        position = row.positions[0]
        assert position.symbol == "AAPL"
        assert position.quantity == Decimal("10")
        assert position.avg_cost == Decimal("100")
        # Every one of these comes from the router's real 120 quote.
        assert position.current_value == Decimal("1200")
        assert position.unrealized_pnl == Decimal("200")
        assert row.total_equity == Decimal("100200")
        assert row.total_unrealized_pnl == Decimal("200")


@pytest.mark.asyncio
async def test_a_scheduled_snapshot_reads_back_through_the_existing_history_endpoint():
    """A scheduler-written row is a first-class snapshot: the same
    GET .../portfolio/history D027 built serves it unchanged."""
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            headers = {"Authorization": f"Bearer {token}"}
            await _buy(client, token, broker_id)

            router = MarketDataRouter([FakeSymbolProvider({"AAPL": Decimal("120")})])
            outcome = await capture_scheduled_snapshot(
                broker_id, session, market_data_router=router
            )
            assert outcome.status is ScheduledSnapshotStatus.CAPTURED

            response = await client.get(
                f"/brokers/{broker_id}/portfolio/history", headers=headers
            )

        assert response.status_code == 200, response.text
        snapshots = response.json()["snapshots"]
        assert len(snapshots) == 1
        assert snapshots[0]["id"] == str(outcome.snapshot_id)
        assert Decimal(snapshots[0]["total_equity"]) == Decimal("100200")
        assert Decimal(snapshots[0]["positions"][0]["current_value"]) == Decimal("1200")


@pytest.mark.asyncio
async def test_an_unavailable_quote_skips_the_broker_and_writes_nothing():
    """The central no-fabrication guarantee of this phase: rather than
    valuing the position at avg_cost, at zero, or at the last known price,
    the cycle records why it could not value it and leaves an honest gap in
    the append-only history."""
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, broker_id)

        router = MarketDataRouter(
            [FakeSymbolProvider({"AAPL": DataUnavailableError("halted, no quote")})]
        )
        outcome = await capture_scheduled_snapshot(broker_id, session, market_data_router=router)

        assert outcome.status is ScheduledSnapshotStatus.SKIPPED_MARKET_DATA_UNAVAILABLE
        assert outcome.snapshot_id is None
        assert outcome.unpriced_symbols == ("AAPL",)
        assert outcome.detail is not None
        assert "NO_DATA_AVAILABLE" in outcome.detail
        assert "halted, no quote" in outcome.detail

        assert await _snapshot_rows(session, broker_id) == []


@pytest.mark.asyncio
async def test_one_unpriceable_symbol_blocks_the_whole_snapshot_not_just_that_position():
    """A partial valuation would be worse than no valuation: a row missing
    one holding is indistinguishable, forever, from a row where that
    holding was genuinely closed."""
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, broker_id)
            await _buy(
                client,
                token,
                broker_id,
                symbol="MSFT",
                quantity="5",
                price="200",
                stop="190",
                marks={"AAPL": "100"},
            )

        router = MarketDataRouter(
            [
                FakeSymbolProvider(
                    {"AAPL": Decimal("120"), "MSFT": DataUnavailableError("no quote")}
                )
            ]
        )
        outcome = await capture_scheduled_snapshot(broker_id, session, market_data_router=router)

        assert outcome.status is ScheduledSnapshotStatus.SKIPPED_MARKET_DATA_UNAVAILABLE
        assert outcome.unpriced_symbols == ("MSFT",)
        assert await _snapshot_rows(session, broker_id) == []


@pytest.mark.asyncio
async def test_no_configured_market_data_skips_a_broker_that_holds_positions():
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, broker_id)

        outcome = await capture_scheduled_snapshot(broker_id, session, market_data_router=None)

        assert outcome.status is ScheduledSnapshotStatus.SKIPPED_MARKET_DATA_NOT_CONFIGURED
        assert outcome.unpriced_symbols == ("AAPL",)
        assert outcome.detail is not None and outcome.detail.startswith("NOT_CONFIGURED:")
        assert await _snapshot_rows(session, broker_id) == []


@pytest.mark.asyncio
async def test_a_broker_with_no_account_row_is_skipped_not_seeded_with_default_cash():
    """Unlike GET .../portfolio, the scheduler never falls back to
    Settings.paper_broker_starting_cash - that would write a permanent
    record of a cash balance no broker_accounts row ever asserted."""
    async with (
        db_session() as session,
        paper_broker_row(session) as broker_id,
    ):
        # A brand-new broker row, never traded against: no broker_accounts row.
        assert broker_id not in await eligible_broker_ids(session)

        outcome = await capture_scheduled_snapshot(broker_id, session, market_data_router=None)

        assert outcome.status is ScheduledSnapshotStatus.SKIPPED_NO_BROKER_ACCOUNT
        assert await _snapshot_rows(session, broker_id) == []


@pytest.mark.asyncio
async def test_a_full_cycle_captures_one_broker_while_skipping_another():
    """One broker's data gap must not abort the rest of the cycle - each
    broker gets its own session and its own outcome."""
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as good_broker_id,
        paper_broker_row(session) as bad_broker_id,
        broker_grant(session, user_id=user_id, broker_id=good_broker_id),
        broker_grant(session, user_id=user_id, broker_id=bad_broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, good_broker_id)
            await _buy(
                client, token, bad_broker_id, symbol="TSLA", quantity="4", price="250", stop="240"
            )

        router = MarketDataRouter(
            [FakeSymbolProvider({"AAPL": Decimal("120"), "TSLA": DataUnavailableError("no quote")})]
        )
        result = await run_snapshot_cycle(get_session_factory(), market_data_router=router)

        by_broker = {o.broker_id: o for o in result.outcomes}
        assert by_broker[good_broker_id].status is ScheduledSnapshotStatus.CAPTURED
        assert (
            by_broker[bad_broker_id].status
            is ScheduledSnapshotStatus.SKIPPED_MARKET_DATA_UNAVAILABLE
        )

        assert len(await _snapshot_rows(session, good_broker_id)) == 1
        assert await _snapshot_rows(session, bad_broker_id) == []


@pytest.mark.asyncio
async def test_the_running_scheduler_task_actually_writes_a_snapshot_on_its_timer():
    """End-to-end proof that the asyncio loop itself works: start the real
    scheduler, let it fire on its own, and confirm a real row landed - not
    just that run_once() would have worked if called."""
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, broker_id)

        scheduler = PortfolioSnapshotScheduler(
            get_session_factory(),
            market_data_router=MarketDataRouter([FakeSymbolProvider({"AAPL": Decimal("120")})]),
            interval_seconds=1,
        )
        scheduler.start()
        assert scheduler.running
        try:
            # The loop runs one cycle immediately on start (see the class
            # docstring); poll rather than sleeping a fixed duration.
            for _ in range(100):
                await asyncio.sleep(0.05)
                if await _snapshot_rows(session, broker_id):
                    break
        finally:
            await scheduler.stop()

        assert not scheduler.running
        rows = await _snapshot_rows(session, broker_id)
        assert len(rows) >= 1
        assert rows[0].total_equity == Decimal("100200")


@pytest.mark.asyncio
async def test_stopping_a_scheduler_that_was_never_started_is_a_no_op():
    scheduler = PortfolioSnapshotScheduler(
        get_session_factory(), market_data_router=None, interval_seconds=60
    )
    assert not scheduler.running
    await scheduler.stop()
    assert not scheduler.running


@pytest.mark.asyncio
async def test_starting_twice_is_rejected_rather_than_silently_double_scheduling():
    scheduler = PortfolioSnapshotScheduler(
        get_session_factory(), market_data_router=None, interval_seconds=3600
    )
    scheduler.start()
    try:
        with pytest.raises(RuntimeError, match="called twice"):
            scheduler.start()
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_eligible_brokers_are_exactly_those_with_a_broker_accounts_row():
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as traded_broker_id,
        paper_broker_row(session) as untouched_broker_id,
        broker_grant(session, user_id=user_id, broker_id=traded_broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, traded_broker_id, quantity="1")

        eligible = await eligible_broker_ids(session)
        assert traded_broker_id in eligible
        assert untouched_broker_id not in eligible
        assert all(isinstance(b, uuid.UUID) for b in eligible)
