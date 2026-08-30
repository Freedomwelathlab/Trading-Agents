"""Integration tests against a real Postgres for Phase 38's cross-worker
safety on the snapshot scheduler (docs/DECISIONS.md D047).

These are DB-backed for a reason that unit tests cannot substitute for: the
whole claim is about *Postgres advisory lock semantics between two distinct
connections*. tests/portfolio/test_cycle_lock.py proves the release
discipline in isolation; what has to be proven here is the consequence -
that a contended cycle enumerates no broker, calls no vendor, and writes no
row, while an uncontended one writes exactly the row it always did.

The "other worker" is not simulated. It is a second, genuinely separate
raw asyncpg connection taken from the real engine, on which the real
`pg_try_advisory_lock` is really taken and really held for the duration of
the assertion. That is the identical mechanism a second uvicorn worker
would use.

Fixtures are reused from tests/api/test_trades.py and the vendor double
from tests/portfolio/test_scheduler.py, exactly as
tests/api/test_snapshot_scheduler.py and
tests/api/test_snapshot_scheduler_market_hours.py do. The only test double
anywhere is at the market-data vendor boundary; the scheduler, the lock,
the router, the snapshot computation, the persistence layer and Postgres
are all real.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from apps.api.app.auth.permissions import Permission
from apps.api.app.db.base import get_session_factory
from apps.api.app.db.models import PortfolioSnapshotRow
from apps.api.app.marketdata.router import MarketDataRouter
from apps.api.app.portfolio.cycle_lock import (
    SNAPSHOT_LOCK_CLASSID,
    SNAPSHOT_LOCK_OBJID,
    SnapshotCycleLock,
    SnapshotCycleLockDecision,
)
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

WEEKDAY = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
"""A real Monday, so the D042 weekend gate can never be the thing under
test here. Asserted below rather than trusted."""


def test_the_injected_instant_really_is_a_weekday():
    assert WEEKDAY.strftime("%A") == "Monday"


@asynccontextmanager
async def other_worker_holding_the_lock():
    """A second, genuinely independent database session that takes and
    holds the real advisory lock - standing in for a sibling uvicorn worker
    that won this interval.

    Nothing about this is mocked: it is the same `pg_try_advisory_lock`
    call on the same key pair over a different backend connection, which is
    exactly what a second process does. The session is closed on exit,
    which is itself the release path a killed worker would take.
    """
    factory = get_session_factory()
    async with factory() as holder:
        acquired = await holder.scalar(
            text("SELECT pg_try_advisory_lock(:classid, :objid)"),
            {"classid": SNAPSHOT_LOCK_CLASSID, "objid": SNAPSHOT_LOCK_OBJID},
        )
        assert acquired is True, (
            "the stand-in worker could not take the lock, so this test would "
            "prove nothing about contention"
        )
        try:
            yield holder
        finally:
            await holder.scalar(
                text("SELECT pg_advisory_unlock(:classid, :objid)"),
                {"classid": SNAPSHOT_LOCK_CLASSID, "objid": SNAPSHOT_LOCK_OBJID},
            )


async def _buy(client, token, broker_id):
    """One real filled position through the real trade endpoint - risk
    engine, OMS, paper broker and persistence included."""
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


def _outcome_for(result, broker_id):
    """This broker's outcome out of the cycle's, so an assertion never
    depends on what else happens to live in the database. A cycle covers
    every eligible broker, and these tests only ever make a claim about the
    one they created."""
    matches = [o for o in result.outcomes if o.broker_id == broker_id]
    assert len(matches) == 1, f"expected exactly one outcome for {broker_id}, got {matches}"
    return matches[0]


async def _snapshot_rows(session, broker_id):
    return (
        (
            await session.execute(
                select(PortfolioSnapshotRow)
                .where(PortfolioSnapshotRow.broker_id == broker_id)
                .order_by(PortfolioSnapshotRow.captured_at.asc())
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_a_cycle_skips_cleanly_when_another_worker_holds_the_lock():
    """The core D047 claim. A broker that would definitely be snapshotted
    is not, because a real second connection holds the real lock - and the
    skip costs no vendor call and writes no row."""
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, broker_id)

        # Sanity: without contention this broker would definitely be worked.
        assert broker_id in await eligible_broker_ids(session)
        assert await open_position_symbols(session, broker_id) == ["AAPL"]

        provider = FakeSymbolProvider({"AAPL": Decimal("120")})
        async with other_worker_holding_the_lock():
            result = await run_snapshot_cycle(
                get_session_factory(),
                market_data_router=MarketDataRouter([provider]),
                market_hours_gate=MarketHoursGate(),
                as_of=WEEKDAY,
                cycle_lock=SnapshotCycleLock(),
            )

        assert result.lock is SnapshotCycleLockDecision.SKIPPED_LOCK_HELD
        assert result.lock_skipped is True
        assert result.ran is False
        # A lock skip is NOT a market-hours skip; the gate said run.
        assert result.market_hours is MarketHoursDecision.RUN
        assert result.gated is False
        # No broker was enumerated at all, not merely this one.
        assert result.outcomes == ()
        assert result.captured_count == 0
        # The two things that actually cost something, both avoided.
        assert provider.calls == []
        assert await _snapshot_rows(session, broker_id) == []


@pytest.mark.asyncio
async def test_the_identical_cycle_captures_normally_when_uncontended():
    """Control for the test above: proves the skip was the lock's doing and
    not a broken setup, and that the single-worker path is unchanged."""
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
            as_of=WEEKDAY,
            cycle_lock=SnapshotCycleLock(),
        )

        assert result.lock is SnapshotCycleLockDecision.ACQUIRED
        assert result.lock_skipped is False
        assert result.ran is True
        assert _outcome_for(result, broker_id).captured is True
        assert "AAPL" in provider.calls

        rows = await _snapshot_rows(session, broker_id)
        assert len(rows) == 1
        # 100000 - 10*100 cash + 10*120 marked = 100200 - the same figure
        # the pre-D047 cycle produced for this exact setup.
        assert rows[0].total_equity == Decimal("100200")


@pytest.mark.asyncio
async def test_the_lock_is_released_so_the_very_next_cycle_captures():
    """A lock that leaked would silently stop history for the life of the
    process, which is worse than the duplicate rows D047 set out to fix.
    Two sequential cycles must both capture."""
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, broker_id)

        for _ in range(2):
            result = await run_snapshot_cycle(
                get_session_factory(),
                market_data_router=MarketDataRouter(
                    [FakeSymbolProvider({"AAPL": Decimal("120")})]
                ),
                market_hours_gate=MarketHoursGate(),
                as_of=WEEKDAY,
                cycle_lock=SnapshotCycleLock(),
            )
            assert result.lock is SnapshotCycleLockDecision.ACQUIRED
            assert _outcome_for(result, broker_id).captured is True

        assert len(await _snapshot_rows(session, broker_id)) == 2

        # And the lock really is free afterwards, not merely unobserved:
        # a fresh contender can take it.
        async with other_worker_holding_the_lock():
            pass


@pytest.mark.asyncio
async def test_two_concurrent_cycles_write_exactly_one_row_between_them():
    """The duplicate-row bug itself, reproduced as closely as one process
    can: two cycles racing over the same broker and the same database.
    Exactly one must capture; the other must record SKIPPED_LOCK_HELD.

    This is the assertion that fails without D047 - it would find two
    rows."""
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, broker_id)

        async def one_cycle():
            return await run_snapshot_cycle(
                get_session_factory(),
                market_data_router=MarketDataRouter(
                    [FakeSymbolProvider({"AAPL": Decimal("120")})]
                ),
                market_hours_gate=MarketHoursGate(),
                as_of=WEEKDAY,
                cycle_lock=SnapshotCycleLock(),
            )

        first, second = await asyncio.gather(one_cycle(), one_cycle())

        decisions = sorted(r.lock.value for r in (first, second))
        assert decisions == ["acquired", "skipped_lock_held"]
        # Exactly one of the two racing cycles produced an outcome for this
        # broker at all - the loser produced none, for anyone.
        captured = [r for r in (first, second) if r.outcomes]
        assert len(captured) == 1
        assert _outcome_for(captured[0], broker_id).captured is True
        assert len(await _snapshot_rows(session, broker_id)) == 1


@pytest.mark.asyncio
async def test_a_disabled_lock_restores_the_pre_d047_duplicate_behaviour():
    """The escape hatch has to work end to end, and demonstrating what it
    restores is also the clearest evidence the lock is what prevents
    duplicates: with it off, a contended cycle happily writes its own row
    alongside the holder's."""
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, broker_id)

        async with other_worker_holding_the_lock():
            result = await run_snapshot_cycle(
                get_session_factory(),
                market_data_router=MarketDataRouter(
                    [FakeSymbolProvider({"AAPL": Decimal("120")})]
                ),
                market_hours_gate=MarketHoursGate(),
                as_of=WEEKDAY,
                cycle_lock=SnapshotCycleLock(enabled=False),
            )

        assert result.lock is SnapshotCycleLockDecision.LOCK_DISABLED
        assert result.lock_skipped is False
        assert _outcome_for(result, broker_id).captured is True
        assert len(await _snapshot_rows(session, broker_id)) == 1


@pytest.mark.asyncio
async def test_omitting_the_lock_entirely_preserves_pre_d047_behavior():
    """Every call site predating Phase 38 passes no lock at all. That must
    keep meaning "run unconditionally", contended or not."""
    async with (
        db_session() as session,
        active_user(session, permissions=ALL_PERMS) as (user_id, email),
        paper_broker_row(session) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        async with api_client() as client:
            token = await get_token(client, email)
            await _buy(client, token, broker_id)

        async with other_worker_holding_the_lock():
            result = await run_snapshot_cycle(
                get_session_factory(),
                market_data_router=MarketDataRouter(
                    [FakeSymbolProvider({"AAPL": Decimal("120")})]
                ),
                market_hours_gate=MarketHoursGate(),
                as_of=WEEKDAY,
            )

        assert result.lock is SnapshotCycleLockDecision.LOCK_DISABLED
        assert _outcome_for(result, broker_id).captured is True
        assert len(await _snapshot_rows(session, broker_id)) == 1


@pytest.mark.asyncio
async def test_the_weekend_gate_still_short_circuits_before_the_lock():
    """Ordering matters: the whole value of D042 is that a weekend cycle
    costs zero database round-trips. Taking the lock first would spend one
    every interval, all weekend, in every worker."""
    saturday = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    assert saturday.strftime("%A") == "Saturday"

    result = await run_snapshot_cycle(
        get_session_factory(),
        market_data_router=MarketDataRouter([FakeSymbolProvider({})]),
        market_hours_gate=MarketHoursGate(),
        as_of=saturday,
        cycle_lock=SnapshotCycleLock(),
    )

    assert result.market_hours is MarketHoursDecision.SKIP_WEEKEND
    assert result.gated is True
    # Not SKIPPED_LOCK_HELD, and not ACQUIRED: the lock was never reached.
    assert result.lock is SnapshotCycleLockDecision.LOCK_DISABLED
    assert result.ran is False

    # And the lock is demonstrably still free, i.e. the gated path really
    # did not touch it.
    async with other_worker_holding_the_lock():
        pass


@pytest.mark.asyncio
async def test_the_scheduler_threads_its_lock_into_every_cycle():
    """The wiring, not just the function: a PortfolioSnapshotScheduler
    constructed with a lock must actually use it, or main.py's
    configuration would be decorative."""
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
            interval_seconds=3600,
            market_hours_gate=MarketHoursGate(enabled=False),
            clock=lambda: WEEKDAY,
            cycle_lock=SnapshotCycleLock(),
        )

        async with other_worker_holding_the_lock():
            contended = await scheduler.run_once()
        assert contended.lock is SnapshotCycleLockDecision.SKIPPED_LOCK_HELD
        assert await _snapshot_rows(session, broker_id) == []

        uncontended = await scheduler.run_once()
        assert uncontended.lock is SnapshotCycleLockDecision.ACQUIRED
        assert len(await _snapshot_rows(session, broker_id)) == 1


@pytest.mark.asyncio
async def test_a_scheduler_constructed_without_a_lock_does_not_lock():
    """Backwards compatibility of the constructor, matching how the gate
    behaves when omitted."""
    scheduler = PortfolioSnapshotScheduler(
        get_session_factory(),
        market_data_router=None,
        interval_seconds=3600,
        clock=lambda: WEEKDAY,
    )

    async with other_worker_holding_the_lock():
        result = await scheduler.run_once()

    assert result.lock is SnapshotCycleLockDecision.LOCK_DISABLED
    assert result.ran is True
