"""Integration tests against a real Postgres for the live-order reconciler
(Phase 49, docs/DECISIONS.md D066).

NO REAL BROKER IS EVER CONTACTED AND LIVE TRADING IS NEVER ENABLED. Every
`Settings` object in this file is the repository default; the live
execution path is supplied as a production `LiveBrokerAdapter` driven by a
fake SDK client with no network access, exactly the Phase 43 pattern
(tests/api/test_live_trades.py). `build_live_broker_adapter()` is never
called and no `LONGPORT_LIVE_*` credential is ever set.

These are DB-backed for a reason unit tests cannot substitute for. The
claims here are all about what happens to real rows: that an unconfirmed
order is really recorded and really committed despite the request rolling
back, that a resolved order really moves status and really gains a `fills`
row built from the broker's own figures, and that a second worker holding
the real advisory lock really stops a second cycle from touching any of it.
tests/execution/test_reconciliation.py proves the classification logic in
isolation; nothing there proves any of this.

The only test double anywhere is at the broker SDK boundary. The
reconciler, the adapter, the OMS, the persistence layer, the lock and
Postgres are all the production code.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, text

from apps.api.app.api.dependencies import get_live_broker_adapter
from apps.api.app.auth.permissions import Permission
from apps.api.app.db.base import get_session_factory
from apps.api.app.db.models import BrokerKind, OrderStatus
from apps.api.app.db.models import Fill as FillRow
from apps.api.app.db.models import Order as OrderRow
from apps.api.app.execution.live_broker import LiveBrokerAdapter
from apps.api.app.execution.reconciliation import (
    LiveOrderReconciler,
    ReconciliationCycleStatus,
    ReconciliationStatus,
    build_reconciler_cycle_lock,
    run_reconciliation_cycle,
    unresolved_live_orders,
)
from apps.api.app.main import app
from apps.api.app.portfolio.cycle_lock import (
    RECONCILER_LOCK_OBJID,
    SNAPSHOT_LOCK_CLASSID,
    SnapshotCycleLockDecision,
)
from tests.api.test_trades import _get_token as get_token
from tests.api.test_trades import (
    active_user,
    api_client,
    broker_grant,
    db_session,
    paper_broker_row,
)
from tests.execution.test_live_broker import _Balance, _OrderDetail, _Position
from tests.execution.test_reconciliation import MappedLiveTradeClient

__all__ = ["active_user", "api_client", "broker_grant", "db_session", "paper_broker_row"]

BOTH_TRADE_PERMISSIONS = (
    Permission.SUBMIT_PAPER_TRADE.value,
    Permission.SUBMIT_LIVE_TRADE.value,
)

UNFILLED = _OrderDetail(status="New", executed_quantity="0", executed_price=None)
"""What the broker says when it has accepted an order but not executed it -
the exact condition that produces `502 LIVE_ORDER_UNCONFIRMED` and, as of
this phase, a `submitted_unconfirmed` row."""


def _adapter(**kwargs: object) -> LiveBrokerAdapter:
    return LiveBrokerAdapter(MappedLiveTradeClient(**kwargs))  # type: ignore[arg-type]


class _override_live_broker:
    """Installs a fake live execution path for the duration of a block, via
    FastAPI's dependency_overrides - so no Settings value is touched and
    LIVE_TRADING_ENABLED stays false throughout. Same mechanism as
    tests/api/test_live_trades.py."""

    def __init__(self, adapter: object | None) -> None:
        self._adapter = adapter

    def __enter__(self) -> None:
        app.dependency_overrides[get_live_broker_adapter] = lambda: self._adapter

    def __exit__(self, *exc: object) -> None:
        del app.dependency_overrides[get_live_broker_adapter]


@asynccontextmanager
async def other_worker_holding_the_reconciler_lock():
    """A second, genuinely independent database connection holding the REAL
    advisory lock on the reconciler's own key - standing in for a sibling
    uvicorn worker that won this interval. Nothing is mocked: same
    `pg_try_advisory_lock`, different backend connection, which is exactly
    what a second process does. Mirrors
    tests/api/test_snapshot_scheduler_multiworker.py's helper."""
    factory = get_session_factory()
    async with factory() as holder:
        acquired = await holder.scalar(
            text("SELECT pg_try_advisory_lock(:classid, :objid)"),
            {"classid": SNAPSHOT_LOCK_CLASSID, "objid": RECONCILER_LOCK_OBJID},
        )
        assert acquired is True, (
            "the stand-in worker could not take the lock, so this test would prove "
            "nothing about contention"
        )
        try:
            yield holder
        finally:
            await holder.scalar(
                text("SELECT pg_advisory_unlock(:classid, :objid)"),
                {"classid": SNAPSHOT_LOCK_CLASSID, "objid": RECONCILER_LOCK_OBJID},
            )


async def _place_unconfirmed_live_order(client, token, broker_id, *, quantity="10"):
    """Drive a REAL live trade through the REAL endpoint against a broker
    that accepts but does not execute, and assert it produced the Phase 43
    502. This is how the row under test comes into existence - it is never
    hand-inserted, so these tests also prove the producing half of D066."""
    response = await client.post(
        f"/brokers/{broker_id}/trades",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "symbol": "AAPL",
            "side": "buy",
            "quantity": quantity,
            "estimated_price": "100",
            "stop_price": "95",
            "confirm": True,
        },
    )
    assert response.status_code == 502, response.text
    assert "LIVE_ORDER_UNCONFIRMED" in response.json()["detail"]
    return response


async def _orders(session, broker_id):
    return (
        (
            await session.execute(
                select(OrderRow)
                .where(OrderRow.broker_id == broker_id)
                .order_by(OrderRow.submitted_at.asc())
            )
        )
        .scalars()
        .all()
    )


async def _reload_order(order_id):
    """Re-read one order on a GENUINELY FRESH session.

    Not a convenience. `apps/api/app/db/base.py` builds its sessionmaker
    with `expire_on_commit=False`, so an object already in a session's
    identity map is NOT refreshed by that session's own commit - a re-select
    would hand back the same stale Python object and an assertion against it
    would prove nothing about what is in Postgres. Every post-reconciliation
    assertion in this file therefore reads through a new session, which also
    makes each one an independent check that the reconciler COMMITTED rather
    than merely flushed.
    """
    factory = get_session_factory()
    async with factory() as fresh:
        return (
            await fresh.execute(select(OrderRow).where(OrderRow.id == order_id))
        ).scalar_one()


async def _fills_for(order_id):
    """Fresh session, for the same reason as `_reload_order` above."""
    factory = get_session_factory()
    async with factory() as fresh:
        return (
            (await fresh.execute(select(FillRow).where(FillRow.order_id == order_id)))
            .scalars()
            .all()
        )


async def _cleanup_orders(session, broker_id):
    """`paper_broker_row` already deletes this broker's orders and fills on
    exit, but these tests commit mid-test on a separate session, so the
    fixture's session may hold a stale identity map. Deleting explicitly
    keeps each test independent of that."""
    await session.execute(
        delete(FillRow).where(
            FillRow.order_id.in_(select(OrderRow.id).where(OrderRow.broker_id == broker_id))
        )
    )
    await session.execute(delete(OrderRow).where(OrderRow.broker_id == broker_id))
    await session.commit()


@asynccontextmanager
async def live_broker_with_unconfirmed_order(*, quantity="10"):
    """The shared setup for every test below: a real live-kind broker, a
    granted user, and one REAL `submitted_unconfirmed` order row produced by
    the real trade endpoint. Yields `(session, broker_id, order)`."""
    async with (
        db_session() as session,
        active_user(session, permissions=BOTH_TRADE_PERMISSIONS) as (user_id, email),
        paper_broker_row(session, kind=BrokerKind.LIVE) as broker_id,
        broker_grant(session, user_id=user_id, broker_id=broker_id),
    ):
        try:
            async with api_client() as client:
                token = await get_token(client, email)
                with _override_live_broker(
                    _adapter(
                        details={"LB-ORDER-1": UNFILLED},
                        balances=[_Balance("USD", "100000")],
                        positions=[_Position("AAPL", "0")],
                    )
                ):
                    await _place_unconfirmed_live_order(
                        client, token, broker_id, quantity=quantity
                    )

            orders = await _orders(session, broker_id)
            assert len(orders) == 1, f"expected exactly one order row, got {orders}"
            yield session, broker_id, orders[0]
        finally:
            await _cleanup_orders(session, broker_id)


# --- the producing half: an unconfirmed live order is actually recorded ---


@pytest.mark.asyncio
async def test_an_unconfirmed_live_order_is_recorded_and_survives_the_502():
    """The Phase 43 gap itself. Before this phase the 502 rolled the
    transaction back and the system forgot it had placed a real order. The
    row must exist AFTER the request failed, on a different session, with
    the broker's own order id on it."""
    async with live_broker_with_unconfirmed_order() as (session, broker_id, order):
        assert order.status is OrderStatus.SUBMITTED_UNCONFIRMED
        assert order.broker_order_id == "LB-ORDER-1"
        assert order.symbol == "AAPL"
        assert order.quantity == Decimal("10")
        # Nothing was concluded yet, and "we have not looked" must not read
        # as "we looked and it was open".
        assert order.broker_status is None
        assert order.reconciled_at is None
        # An approved order that reached a broker was blocked by nothing.
        assert order.risk_block_reason is None
        # And emphatically no fill: none was reported.
        assert await _fills_for(order.id) == []


@pytest.mark.asyncio
async def test_the_recorded_order_is_visible_to_a_completely_separate_session():
    """Proves the row was COMMITTED rather than merely flushed - which is
    the whole reason `_record_unconfirmed_order` commits. A fresh session
    from the pool sees it."""
    async with live_broker_with_unconfirmed_order() as (_session, broker_id, order):
        factory = get_session_factory()
        async with factory() as other:
            found = (
                await other.execute(select(OrderRow).where(OrderRow.id == order.id))
            ).scalar_one()
            assert found.status is OrderStatus.SUBMITTED_UNCONFIRMED
            assert found.broker_order_id == "LB-ORDER-1"


@pytest.mark.asyncio
async def test_an_unconfirmed_order_records_the_quantity_actually_sent():
    """Not the proposal's quantity. These are the same number here, but the
    value comes from the OMS's `submission_context` (the post-Portfolio-
    Manager quantity), which is the only one that was ever sent to a
    venue."""
    async with live_broker_with_unconfirmed_order(quantity="7") as (_s, _b, order):
        assert order.quantity == Decimal("7")


# --- the reconciling half -------------------------------------------------


@pytest.mark.asyncio
async def test_an_order_the_broker_now_reports_filled_is_resolved_with_a_real_fill():
    """The headline behaviour. The order moves to FILLED, records the
    venue's own status string, gains `reconciled_at`, and gets a `fills` row
    built ENTIRELY from the broker's executed quantity and price - not from
    the order's estimated_price of 100."""
    filled_at = datetime(2026, 9, 3, 15, 45, tzinfo=UTC)

    async with live_broker_with_unconfirmed_order() as (session, broker_id, order):
        broker = _adapter(
            details={
                "LB-ORDER-1": _OrderDetail(
                    status="Filled",
                    executed_quantity="10",
                    executed_price="101.25",
                    updated_at=filled_at,
                )
            }
        )

        result = await run_reconciliation_cycle(
            get_session_factory(),
            live_broker=broker,
            cycle_lock=build_reconciler_cycle_lock(),
        )

        assert result.ran is True
        outcome = _outcome_for(result, order.id)
        assert outcome.status is ReconciliationStatus.RESOLVED_FILLED
        assert outcome.broker_status == "Filled"

        refreshed = await _reload_order(order.id)
        assert refreshed.status is OrderStatus.FILLED
        assert refreshed.broker_status == "Filled"
        assert refreshed.reconciled_at is not None

        fills = await _fills_for(order.id)
        assert len(fills) == 1
        # The broker's figures, not the proposal's. estimated_price was 100.
        assert fills[0].fill_price == Decimal("101.25")
        assert fills[0].quantity == Decimal("10")
        assert fills[0].filled_at == filled_at


@pytest.mark.asyncio
async def test_an_order_still_pending_at_the_broker_is_left_completely_alone():
    """No status change, no reconciled_at, no fill - and it stays in the
    reconciler's work queue for next cycle. Aging an order out on a timer is
    exactly what this must not do."""
    async with live_broker_with_unconfirmed_order() as (session, broker_id, order):
        broker = _adapter(details={"LB-ORDER-1": UNFILLED})

        result = await run_reconciliation_cycle(
            get_session_factory(), live_broker=broker, cycle_lock=build_reconciler_cycle_lock()
        )

        assert _outcome_for(result, order.id).status is ReconciliationStatus.STILL_OPEN
        assert result.resolved_count == 0
        assert result.still_open_count == 1

        refreshed = await _reload_order(order.id)
        assert refreshed.status is OrderStatus.SUBMITTED_UNCONFIRMED
        assert refreshed.broker_status is None
        assert refreshed.reconciled_at is None
        assert await _fills_for(order.id) == []

        # Still queued, which is the point of leaving it alone.
        assert order.id in {o.id for o in await unresolved_live_orders(session)}


@pytest.mark.parametrize("raw_status", ["Canceled", "Rejected", "Expired"])
@pytest.mark.asyncio
async def test_an_order_the_broker_closed_unfilled_is_resolved_without_a_fill(raw_status):
    """Cancelled / venue-rejected / expired all resolve to the same terminal
    status, with the venue's OWN word for it preserved verbatim in
    `broker_status` rather than flattened away. Critically: no fill row,
    because there was no execution."""
    async with live_broker_with_unconfirmed_order() as (session, broker_id, order):
        broker = _adapter(
            details={
                "LB-ORDER-1": _OrderDetail(
                    status=raw_status, executed_quantity="0", executed_price=None
                )
            }
        )

        result = await run_reconciliation_cycle(
            get_session_factory(), live_broker=broker, cycle_lock=build_reconciler_cycle_lock()
        )

        outcome = _outcome_for(result, order.id)
        assert outcome.status is ReconciliationStatus.RESOLVED_CLOSED_UNFILLED
        assert outcome.broker_status == raw_status

        refreshed = await _reload_order(order.id)
        assert refreshed.status is OrderStatus.BROKER_CLOSED_UNFILLED
        # The distinction that matters: this is NOT `rejected`, which means
        # THIS system blocked the trade and it never reached a venue.
        assert refreshed.status is not OrderStatus.REJECTED
        assert refreshed.broker_status == raw_status
        assert refreshed.reconciled_at is not None
        assert await _fills_for(order.id) == []


@pytest.mark.asyncio
async def test_a_failing_broker_call_skips_that_order_without_crashing_the_cycle():
    """A failure to OBSERVE is never evidence about the thing observed. The
    order must be untouched and the cycle must complete."""
    async with live_broker_with_unconfirmed_order() as (session, broker_id, order):
        broker = _adapter(raises={"LB-ORDER-1": RuntimeError("connection reset by peer")})

        result = await run_reconciliation_cycle(
            get_session_factory(), live_broker=broker, cycle_lock=build_reconciler_cycle_lock()
        )

        assert result.ran is True  # the cycle survived
        outcome = _outcome_for(result, order.id)
        assert outcome.status is ReconciliationStatus.SKIPPED_BROKER_UNAVAILABLE
        # The vendor's own message, verbatim, not a paraphrase.
        assert "connection reset by peer" in (outcome.detail or "")

        refreshed = await _reload_order(order.id)
        assert refreshed.status is OrderStatus.SUBMITTED_UNCONFIRMED
        assert refreshed.reconciled_at is None
        assert await _fills_for(order.id) == []


@pytest.mark.asyncio
async def test_a_resolved_order_is_never_reconciled_a_second_time():
    """The append-only guarantee, enforced by the UPDATE's `WHERE status =
    'submitted_unconfirmed'` guard rather than by this code being careful.
    A second cycle must not re-ask, must not re-write, and must not write a
    second fill."""
    async with live_broker_with_unconfirmed_order() as (session, broker_id, order):
        client = MappedLiveTradeClient(
            details={
                "LB-ORDER-1": _OrderDetail(
                    status="Filled", executed_quantity="10", executed_price="101.25"
                )
            }
        )
        broker = LiveBrokerAdapter(client)  # type: ignore[arg-type]

        first = await run_reconciliation_cycle(
            get_session_factory(), live_broker=broker, cycle_lock=build_reconciler_cycle_lock()
        )
        assert _outcome_for(first, order.id).status is ReconciliationStatus.RESOLVED_FILLED
        calls_after_first = list(client.status_calls)

        second = await run_reconciliation_cycle(
            get_session_factory(), live_broker=broker, cycle_lock=build_reconciler_cycle_lock()
        )

        # It is not merely idempotent - the order is not even looked at,
        # because it is no longer in the work query at all.
        assert not [o for o in second.outcomes if o.order_id == order.id]
        assert client.status_calls == calls_after_first

        assert len(await _fills_for(order.id)) == 1


@pytest.mark.asyncio
async def test_the_work_query_ignores_orders_on_a_paper_broker():
    """Structural, not conventional (spec Sec51). Sending a paper order's id
    to a real trading venue would be asking a live account about a simulated
    order. A paper broker's rows must never appear in the work set even if
    one somehow reached this status."""
    async with (
        db_session() as session,
        paper_broker_row(session, kind=BrokerKind.PAPER) as paper_id,
    ):
        session.add(
            OrderRow(
                broker_id=paper_id,
                symbol="AAPL",
                side="buy",
                quantity=Decimal("10"),
                estimated_price=Decimal("100"),
                status=OrderStatus.SUBMITTED_UNCONFIRMED,
                broker_order_id=f"PAPER-{uuid.uuid4()}",
            )
        )
        await session.commit()

        try:
            queued = await unresolved_live_orders(session)
            assert paper_id not in {o.broker_id for o in queued}
        finally:
            await _cleanup_orders(session, paper_id)


# --- multi-worker safety --------------------------------------------------


@pytest.mark.asyncio
async def test_a_cycle_skips_cleanly_when_another_worker_holds_the_lock():
    """The D047 mechanism applied to this job. A real second connection
    holds the real lock, so this cycle must make NO broker call and change
    NO row - which matters more here than for snapshots, because the call it
    avoids is to a real trading venue."""
    async with live_broker_with_unconfirmed_order() as (session, broker_id, order):
        client = MappedLiveTradeClient(
            details={
                "LB-ORDER-1": _OrderDetail(
                    status="Filled", executed_quantity="10", executed_price="101.25"
                )
            }
        )

        async with other_worker_holding_the_reconciler_lock():
            result = await run_reconciliation_cycle(
                get_session_factory(),
                live_broker=LiveBrokerAdapter(client),  # type: ignore[arg-type]
                cycle_lock=build_reconciler_cycle_lock(),
            )

        assert result.lock is SnapshotCycleLockDecision.SKIPPED_LOCK_HELD
        assert result.status is ReconciliationCycleStatus.SKIPPED_LOCK_HELD
        assert result.ran is False
        assert result.outcomes == ()
        # The two things that actually cost something, both avoided.
        assert client.status_calls == []

        refreshed = await _reload_order(order.id)
        assert refreshed.status is OrderStatus.SUBMITTED_UNCONFIRMED


@pytest.mark.asyncio
async def test_two_concurrent_cycles_resolve_the_order_exactly_once():
    """The duplicate-work bug itself, reproduced as closely as one process
    can: two cycles racing over the same unresolved order. Exactly one may
    resolve it, and there must be exactly one fill row afterwards. This is
    the assertion that fails without the lock."""
    async with live_broker_with_unconfirmed_order() as (session, broker_id, order):
        detail = _OrderDetail(
            status="Filled", executed_quantity="10", executed_price="101.25"
        )

        async def one_cycle():
            return await run_reconciliation_cycle(
                get_session_factory(),
                live_broker=LiveBrokerAdapter(  # type: ignore[arg-type]
                    MappedLiveTradeClient(details={"LB-ORDER-1": detail})
                ),
                cycle_lock=build_reconciler_cycle_lock(),
            )

        first, second = await asyncio.gather(one_cycle(), one_cycle())

        assert sorted(r.lock.value for r in (first, second)) == [
            "acquired",
            "skipped_lock_held",
        ]
        winners = [r for r in (first, second) if r.outcomes]
        assert len(winners) == 1
        assert _outcome_for(winners[0], order.id).status is ReconciliationStatus.RESOLVED_FILLED

        assert len(await _fills_for(order.id)) == 1
        assert (await _reload_order(order.id)).status is OrderStatus.FILLED


@pytest.mark.asyncio
async def test_the_reconciler_lock_does_not_block_the_snapshot_scheduler():
    """The reason the two jobs take different objids. If they shared a key,
    every reconciliation cycle would silently skip whenever a snapshot was
    in flight - and it would look exactly like 'there was nothing to do'."""
    from tests.api.test_snapshot_scheduler_multiworker import (
        other_worker_holding_the_lock as other_worker_holding_the_snapshot_lock,
    )

    async with live_broker_with_unconfirmed_order() as (session, broker_id, order):
        client = MappedLiveTradeClient(
            details={
                "LB-ORDER-1": _OrderDetail(
                    status="Filled", executed_quantity="10", executed_price="101.25"
                )
            }
        )

        # A worker holding the SNAPSHOT lock must not impede the reconciler.
        async with other_worker_holding_the_snapshot_lock():
            result = await run_reconciliation_cycle(
                get_session_factory(),
                live_broker=LiveBrokerAdapter(client),  # type: ignore[arg-type]
                cycle_lock=build_reconciler_cycle_lock(),
            )

        assert result.lock is SnapshotCycleLockDecision.ACQUIRED
        assert _outcome_for(result, order.id).status is ReconciliationStatus.RESOLVED_FILLED


@pytest.mark.asyncio
async def test_the_lock_is_released_so_the_next_cycle_can_run():
    """A leaked lock would silently stop reconciliation for the life of the
    process - worse than the duplicate work it exists to prevent."""
    async with live_broker_with_unconfirmed_order() as (session, broker_id, order):
        broker = _adapter(details={"LB-ORDER-1": UNFILLED})

        for _ in range(2):
            result = await run_reconciliation_cycle(
                get_session_factory(),
                live_broker=broker,
                cycle_lock=build_reconciler_cycle_lock(),
            )
            assert result.lock is SnapshotCycleLockDecision.ACQUIRED

        # And it really is free afterwards, not merely unobserved.
        async with other_worker_holding_the_reconciler_lock():
            pass


# --- the scheduler wrapper ------------------------------------------------


@pytest.mark.asyncio
async def test_the_reconciler_threads_its_lock_into_every_cycle():
    """The wiring, not just the function: main.py's configuration would be
    decorative if the loop did not actually use the lock it was given."""
    async with live_broker_with_unconfirmed_order() as (session, broker_id, order):
        reconciler = LiveOrderReconciler(
            get_session_factory(),
            live_broker=_adapter(
                details={
                    "LB-ORDER-1": _OrderDetail(
                        status="Filled", executed_quantity="10", executed_price="101.25"
                    )
                }
            ),
            interval_seconds=300,
            cycle_lock=build_reconciler_cycle_lock(),
        )

        async with other_worker_holding_the_reconciler_lock():
            contended = await reconciler.run_once()
        assert contended.lock is SnapshotCycleLockDecision.SKIPPED_LOCK_HELD

        still_unconfirmed = await _reload_order(order.id)
        assert still_unconfirmed.status is OrderStatus.SUBMITTED_UNCONFIRMED

        uncontended = await reconciler.run_once()
        assert uncontended.lock is SnapshotCycleLockDecision.ACQUIRED
        assert _outcome_for(uncontended, order.id).status is ReconciliationStatus.RESOLVED_FILLED

        assert len(await _fills_for(order.id)) == 1


# --- the /health surface --------------------------------------------------


@pytest.mark.asyncio
async def test_health_reports_the_reconciler_disabled_by_default():
    """The committed default, visible without reading logs or the process
    environment. This is the assertion that must fail if anyone ever
    defaults this job to on."""
    async with api_client() as client:
        response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["live_order_reconciler"] == "DISABLED"
    # And the pre-existing keys are untouched.
    assert body["status"] == "ok"
    assert body["live_trading_enabled"] is False


def _outcome_for(result, order_id):
    """This order's outcome out of the cycle's, so no assertion depends on
    what else happens to be in the database. A cycle covers every unresolved
    live order; these tests only ever claim things about their own."""
    matches = [o for o in result.outcomes if o.order_id == order_id]
    assert len(matches) == 1, f"expected exactly one outcome for {order_id}, got {matches}"
    return matches[0]
