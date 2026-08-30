"""Integration tests against a real Postgres instance for Phase 35's
market-hours gating of the snapshot scheduler (docs/DECISIONS.md D042).

These are DB-backed on purpose. The unit tests in
tests/portfolio/test_market_hours.py prove the gate's arithmetic in
isolation; what has to be proven *here*, against real Postgres and a real
broker holding a real filled position, is the consequence: on a weekend no
row is written and no vendor call is made, and on a weekday the identical
setup does write one.

The "as of" clock is INJECTED - a real Saturday and a real Monday instant,
named below and checked against the actual calendar - rather than waited
for. No test here waits for an actual weekend, and none of them pretends
to have observed one.

Fixtures are reused from tests/api/test_trades.py and the vendor double
from tests/portfolio/test_scheduler.py, exactly as
tests/api/test_snapshot_scheduler.py does. The only test double anywhere is
at the market-data vendor boundary; the scheduler, the gate, the router,
the snapshot computation, the persistence layer and Postgres are all real.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from apps.api.app.auth.permissions import Permission
from apps.api.app.db.base import get_session_factory
from apps.api.app.db.models import PortfolioSnapshotRow
from apps.api.app.marketdata.router import MarketDataRouter
from apps.api.app.portfolio.market_hours import MarketHoursDecision, MarketHoursGate
from apps.api.app.portfolio.scheduler import (
    PortfolioSnapshotScheduler,
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

__all__ = ["active_user", "api_client", "broker_grant", "db_session", "paper_broker_row"]

ALL_PERMS = (Permission.SUBMIT_PAPER_TRADE.value, Permission.VIEW_PORTFOLIO.value)

SATURDAY = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
MONDAY = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def test_the_injected_instants_really_are_a_saturday_and_a_monday():
    """Guards every assertion in this file: they all mean nothing if these
    two constants have drifted off the real calendar."""
    assert SATURDAY.strftime("%A") == "Saturday"
    assert MONDAY.strftime("%A") == "Monday"


async def _buy(client, token, broker_id):
    """One real filled position through the real trade endpoint - risk
    engine, OMS, paper broker and persistence included. Nothing here is
    inserted by hand."""
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


@pytest.mark.asyncio
async def test_a_weekend_cycle_writes_nothing_and_makes_no_vendor_call():
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, broker_id)

        # Sanity: this broker is genuinely eligible and genuinely holds a
        # position, so an ungated cycle would definitely do real work.
        assert broker_id in await eligible_broker_ids(session)
        assert await open_position_symbols(session, broker_id) == ["AAPL"]

        provider = FakeSymbolProvider({"AAPL": Decimal("120")})
        result = await run_snapshot_cycle(
            get_session_factory(),
            market_data_router=MarketDataRouter([provider]),
            market_hours_gate=MarketHoursGate(),
            as_of=SATURDAY,
        )

        assert result.market_hours is MarketHoursDecision.SKIP_WEEKEND
        assert result.gated is True
        assert result.outcomes == ()
        assert result.captured_count == 0
        # The entire point of the gate: the vendor was never asked.
        assert provider.calls == []
        assert await _snapshot_rows(session, broker_id) == []


@pytest.mark.asyncio
async def test_the_identical_cycle_on_a_weekday_does_capture_a_real_snapshot():
    """Control for the test above - proves the weekend result was the gate's
    doing and not a broken setup."""
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, broker_id)

        provider = FakeSymbolProvider({"AAPL": Decimal("120")})
        result = await run_snapshot_cycle(
            get_session_factory(),
            market_data_router=MarketDataRouter([provider]),
            market_hours_gate=MarketHoursGate(),
            as_of=MONDAY,
        )

        assert result.market_hours is MarketHoursDecision.RUN
        assert result.gated is False
        assert provider.calls == ["AAPL"]

        rows = await _snapshot_rows(session, broker_id)
        assert len(rows) == 1
        # 100000 - 10*100 cash + 10*120 marked = 100200.
        assert rows[0].total_equity == Decimal("100200")


@pytest.mark.asyncio
async def test_a_disabled_gate_captures_on_a_weekend_exactly_as_it_did_before_d042():
    """The escape hatch has to work end to end, or "configurable" is a claim
    rather than a feature."""
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, broker_id)

        result = await run_snapshot_cycle(
            get_session_factory(),
            market_data_router=MarketDataRouter([FakeSymbolProvider({"AAPL": Decimal("120")})]),
            market_hours_gate=MarketHoursGate(enabled=False),
            as_of=SATURDAY,
        )

        assert result.market_hours is MarketHoursDecision.RUN_GATE_DISABLED
        assert result.gated is False
        assert result.captured_count == 1
        assert len(await _snapshot_rows(session, broker_id)) == 1


@pytest.mark.asyncio
async def test_omitting_the_gate_entirely_preserves_pre_d042_behavior():
    """Every call site predating Phase 35 passes no gate at all. That must
    keep meaning "run unconditionally", weekend or not."""
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, broker_id)

        result = await run_snapshot_cycle(
            get_session_factory(),
            market_data_router=MarketDataRouter([FakeSymbolProvider({"AAPL": Decimal("120")})]),
            as_of=SATURDAY,
        )

        assert result.market_hours is MarketHoursDecision.RUN_GATE_DISABLED
        assert result.captured_count == 1


@pytest.mark.asyncio
async def test_the_running_scheduler_no_ops_all_weekend_then_captures_when_it_reopens():
    """The behavior an operator actually experiences, driven through the real
    asyncio loop: a scheduler whose clock says Saturday ticks repeatedly and
    writes nothing; the same still-running scheduler, once its clock crosses
    into Monday, captures on its next tick.

    The clock is a test-controlled callable. This waits for a *simulated*
    weekend to end, not a real one.
    """
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, broker_id)

        clock_reads: list[datetime] = []
        current = {"now": SATURDAY}

        def clock() -> datetime:
            clock_reads.append(current["now"])
            return current["now"]

        provider = FakeSymbolProvider({"AAPL": Decimal("120")})
        scheduler = PortfolioSnapshotScheduler(
            get_session_factory(),
            market_data_router=MarketDataRouter([provider]),
            interval_seconds=1,
            market_hours_gate=MarketHoursGate(),
            clock=clock,
        )
        scheduler.start()
        try:
            # Let the simulated weekend tick several times on its own.
            for _ in range(200):
                await asyncio.sleep(0.05)
                if len(clock_reads) >= 3:
                    break
            assert len(clock_reads) >= 3, "the scheduler loop never ticked"
            assert await _snapshot_rows(session, broker_id) == []
            assert provider.calls == []

            # The market reopens under the still-running scheduler.
            current["now"] = MONDAY
            for _ in range(200):
                await asyncio.sleep(0.05)
                if await _snapshot_rows(session, broker_id):
                    break
        finally:
            await scheduler.stop()

        rows = await _snapshot_rows(session, broker_id)
        assert len(rows) >= 1
        assert rows[0].total_equity == Decimal("100200")
        assert provider.calls, "a weekday cycle must actually reach the vendor"
