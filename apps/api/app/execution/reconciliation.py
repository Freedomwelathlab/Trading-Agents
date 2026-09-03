"""Reconciliation of accepted-but-unexecuted LIVE orders (Phase 49,
docs/DECISIONS.md D066) - the gap Phase 43/D058 explicitly recorded as not
built, and which docs/TRADING_SAFETY.md still lists under "Not yet enforced
(because not yet built)".

THE GAP THIS CLOSES
-------------------
D058 built the live path so that a live order is placed through
`LiveBrokerAdapter`, and if the broker does not confirm a fill
synchronously the caller gets `502 LIVE_ORDER_UNCONFIRMED` naming the real
broker order id rather than an invented fill. That refusal to fabricate was
right and is unchanged. What was missing is everything after it: nothing
ever went back to ask what actually happened. The order id existed in one
log line and one HTTP response, and the system's own append-only audit
trail had no row for the single most consequential thing it can do - hand a
real venue a real order.

Phase 49 closes that in two halves. `apps/api/app/oms/persistence.py` now
records (and commits) an `orders` row in the new, non-terminal
`SUBMITTED_UNCONFIRMED` status carrying the broker's own order id. This
module is the other half: on an interval, it asks the real broker what
became of each such order and resolves it to whatever the broker actually
reports.

NOTHING HERE MAY EVER INVENT A STATUS OR A FILL
-----------------------------------------------
Every terminal value this module writes comes from a real
`LiveOrderStatus` returned by `LiveBrokerAdapter.get_order_status()`, which
is the SDK's own `order_detail()` response. Specifically:

  * A `Fill` row is written ONLY from the broker's own
    `executed_quantity`/`executed_price`. Never from the order's
    `estimated_price`, never from a mark, never from a partial figure
    completed with a guess.
  * An order the broker still reports as working is LEFT EXACTLY AS IT IS.
    Not aged out, not timed out, not assumed cancelled. An order this
    system cannot resolve stays visibly unresolved, because "we do not
    know" is a true statement and any of the alternatives would be a false
    one recorded permanently.
  * A broker call that FAILS (network, auth, a symbol the venue rejects)
    skips that one order for that cycle and moves on - the same discipline
    `PortfolioSnapshotScheduler` applies to a broker it cannot price
    (D030). A failure to observe is never evidence about the thing being
    observed.

INERT UNLESS LIVE TRADING IS GENUINELY CONFIGURED
-------------------------------------------------
`LIVE_ORDER_RECONCILER_ENABLED` defaults to false, the same fail-closed
posture as every other opt-in background loop here. But even switching it
on does nothing on a default deployment: a cycle short-circuits immediately
with `SKIPPED_NOT_CONFIGURED` unless a real `LiveBrokerAdapter` exists,
which still requires `TRADING_MODE=live` AND `LIVE_TRADING_ENABLED=true`
AND all three `LONGPORT_LIVE_*` credentials (`build_live_broker_adapter()`,
D058). The short-circuit happens BEFORE the database is queried, so an
enabled-but-unconfigured deployment costs one log line per interval and
nothing else. This module never constructs an adapter itself and never
reads a credential - it is handed whatever the lifespan already built, or
`None`.

WHY AN in-process asyncio TASK, AND WHY THE SAME ADVISORY LOCK
--------------------------------------------------------------
Both answers are D030's and D047's, deliberately reused rather than
re-litigated. No new dependency: "run this coroutine every N seconds" is
`asyncio.sleep` in a task owned by the FastAPI lifespan, and cross-worker
exclusion is a non-blocking Postgres session-level advisory lock
(`apps/api/app/portfolio/cycle_lock.py`). This job takes the SAME classid
namespace with its OWN objid (`RECONCILER_LOCK_OBJID`), which is exactly
the second-job case that module's "TWO int4 KEYS" section anticipated - so
a reconciliation cycle and a snapshot cycle never exclude each other, while
two reconciliation cycles in two workers do.

The lock matters more here than it does for snapshots. Two workers
duplicating a snapshot writes two rows in a chart; two workers
independently resolving the same live order means two calls to a real
venue's API and two racing UPDATEs against one row. The UPDATE is also
guarded independently - see `resolve_order` - so correctness does not
depend on the lock alone.
"""

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app.core.logging import get_logger
from apps.api.app.db.models import Broker, BrokerKind, OrderStatus
from apps.api.app.db.models import Fill as FillRow
from apps.api.app.db.models import Order as OrderRow
from apps.api.app.execution.live_broker import (
    LiveBrokerAdapter,
    LiveBrokerError,
    LiveOrderStatus,
)
from apps.api.app.portfolio.cycle_lock import (
    RECONCILER_LOCK_OBJID,
    SnapshotCycleLock,
    SnapshotCycleLockDecision,
)

logger = get_logger(__name__)


def utc_now() -> datetime:
    """The real clock, injectable at every call site below so a test can pin
    `reconciled_at` without patching a module global."""
    return datetime.now(UTC)


def build_reconciler_cycle_lock(*, enabled: bool = True) -> SnapshotCycleLock:
    """The reconciler's cross-worker lock: D047's exact mechanism and
    D047's exact class, on this job's own object key and under this job's
    own log-event prefix.

    A factory rather than a second lock class, because there is nothing
    different about the locking - only about which job is being locked. A
    parallel implementation would be a second thing to get right, and the
    one property that must hold (two workers, one winner) is precisely the
    property already proven for the existing one.
    """
    return SnapshotCycleLock(
        enabled=enabled,
        objid=RECONCILER_LOCK_OBJID,
        job_name="live_order_reconciler",
    )


class ReconciliationStatus(str, Enum):  # noqa: UP042 (str mixin for log/JSON interop)
    """Every way one order's reconciliation attempt can end. Exhaustive and
    typed for the same reason `ScheduledSnapshotStatus` is (D030): "this
    order is still unresolved" must never become indistinguishable from
    "the broker was unreachable when we asked", especially when the subject
    is a real order carrying real money.
    """

    RESOLVED_FILLED = "resolved_filled"
    """The broker reports the order finished WITH an execution. The order
    row moved to FILLED and a `fills` row was written from the broker's own
    executed quantity and price."""

    RESOLVED_CLOSED_UNFILLED = "resolved_closed_unfilled"
    """The broker reports the order finished with NO execution - cancelled,
    expired, or rejected by the venue. The order row moved to
    BROKER_CLOSED_UNFILLED and no fill was written, because there is none.
    The venue's own word for it is recorded in `orders.broker_status`."""

    STILL_OPEN = "still_open"
    """The broker reports the order is still working, or reports a status
    this system does not classify as terminal. NOTHING was changed - not
    the status, not `reconciled_at`. It will be asked about again next
    cycle, indefinitely. Deliberately not a failure: an order that takes
    hours to fill is ordinary."""

    SKIPPED_BROKER_UNAVAILABLE = "skipped_broker_unavailable"
    """The broker call itself failed. This says nothing whatsoever about
    the order, which is exactly why the order is left untouched."""

    SKIPPED_ALREADY_RESOLVED = "skipped_already_resolved"
    """The guarded UPDATE matched no row, i.e. something else resolved this
    order between the read and the write. Benign and expected under
    concurrency; recorded rather than hidden, and never retried in a way
    that could double-write a fill."""


@dataclass(frozen=True)
class ReconciliationOutcome:
    """The typed result of attempting ONE order. Returned, never raised, so
    one order's problem can never abort the rest of the cycle."""

    order_id: uuid.UUID
    broker_order_id: str
    status: ReconciliationStatus
    broker_status: str | None = None
    """The venue's own status string, verbatim, when one was obtained."""
    detail: str | None = None
    """Carries the underlying error text verbatim where one exists, so a log
    reader sees the broker's actual message rather than a paraphrase."""

    @property
    def resolved(self) -> bool:
        return self.status in (
            ReconciliationStatus.RESOLVED_FILLED,
            ReconciliationStatus.RESOLVED_CLOSED_UNFILLED,
        )


class ReconciliationCycleStatus(str, Enum):  # noqa: UP042 (str mixin for log/JSON interop)
    """Why a whole cycle did or did not do work."""

    RAN = "ran"
    SKIPPED_DISABLED = "skipped_disabled"
    """No live path exists, so there is nothing this cycle could ask
    anybody. Distinct from a lock skip and from an empty run."""
    SKIPPED_LOCK_HELD = "skipped_lock_held"


@dataclass(frozen=True)
class ReconciliationCycleResult:
    """One full pass over every unresolved live order - or a record of why
    there was no pass at all."""

    outcomes: tuple[ReconciliationOutcome, ...] = field(default=())
    status: ReconciliationCycleStatus = ReconciliationCycleStatus.RAN
    lock: SnapshotCycleLockDecision = SnapshotCycleLockDecision.LOCK_DISABLED
    detail: str | None = None

    @property
    def ran(self) -> bool:
        return self.status is ReconciliationCycleStatus.RAN

    @property
    def resolved_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.resolved)

    @property
    def still_open_count(self) -> int:
        return sum(
            1 for outcome in self.outcomes if outcome.status is ReconciliationStatus.STILL_OPEN
        )


async def unresolved_live_orders(session: AsyncSession) -> list[OrderRow]:
    """Every order this cycle will ask the broker about: rows that are
    `SUBMITTED_UNCONFIRMED`, carry a real `broker_order_id`, AND belong to a
    broker whose row is `kind=live`.

    All three conditions are enforced in SQL rather than assumed:

      * The status filter is the whole point - a terminal row is finished
        and must never be looked at again.
      * `broker_order_id IS NOT NULL` because the id is the only handle by
        which the broker can be asked. `_record_unconfirmed_order` never
        writes a row without one, so this is belt-and-braces against a
        future writer that might.
      * The join to `brokers` on `kind = live` is the one that actually
        matters for safety. A paper broker's order can never legitimately
        reach this status, and if one somehow did, sending its id to a real
        trading venue would be asking a live account about a simulated
        order. Structural, not conventional (spec Sec51).

    Ordered oldest-first so the longest-outstanding order - the one most
    likely to need a human - is resolved first if a cycle is cut short.
    """
    rows = (
        (
            await session.execute(
                select(OrderRow)
                .join(Broker, Broker.id == OrderRow.broker_id)
                .where(
                    OrderRow.status == OrderStatus.SUBMITTED_UNCONFIRMED,
                    OrderRow.broker_order_id.is_not(None),
                    Broker.kind == BrokerKind.LIVE,
                )
                .order_by(OrderRow.submitted_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def resolve_order(
    session: AsyncSession,
    order: OrderRow,
    status: LiveOrderStatus,
    *,
    now: datetime,
) -> ReconciliationOutcome:
    """Apply ONE broker-reported status to ONE order row.

    THE GUARDED UPDATE
    ------------------
    The UPDATE carries `WHERE id = :id AND status = 'submitted_unconfirmed'`
    and the outcome is decided by its `rowcount`. That single clause is what
    makes this operation safe to run concurrently and safe to run twice:

      * A row that has already been resolved matches nothing, so a terminal
        order can never be rewritten - by this job, by a second worker whose
        advisory lock somehow lapsed, or by a re-run. The append-only
        guarantee `orders` is documented under therefore holds against
        anything except the single provisional->terminal transition, and it
        holds in the DATABASE rather than by this code being careful.
      * The `fills` insert happens only when that UPDATE actually matched,
        so a fill can never be double-written. `fills.order_id` is UNIQUE
        as a second, independent backstop.

    Deliberately does NOT commit; `run_reconciliation_cycle` owns one
    transaction per order so that one order's failure cannot poison
    another's.
    """
    assert order.broker_order_id is not None  # guaranteed by unresolved_live_orders()

    if status.still_open:
        # Nothing is written at all - not even reconciled_at. See the module
        # docstring: an order the venue still calls open is not an event.
        return ReconciliationOutcome(
            order_id=order.id,
            broker_order_id=order.broker_order_id,
            status=ReconciliationStatus.STILL_OPEN,
            broker_status=status.raw_status,
        )

    new_status = (
        OrderStatus.FILLED if status.executed else OrderStatus.BROKER_CLOSED_UNFILLED
    )

    # `CursorResult` (not the base `Result`) is what an UPDATE actually
    # returns and the only one carrying `rowcount`, which this function's
    # entire concurrency-safety argument rests on. Cast rather than
    # `type: ignore` so the reason is legible.
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(OrderRow)
            .where(
                OrderRow.id == order.id,
                # The guard. Never remove it: without it this becomes a
                # general-purpose "rewrite any order" statement.
                OrderRow.status == OrderStatus.SUBMITTED_UNCONFIRMED,
            )
            .values(
                status=new_status,
                broker_status=status.raw_status,
                reconciled_at=now,
            )
        ),
    )

    if result.rowcount == 0:
        return ReconciliationOutcome(
            order_id=order.id,
            broker_order_id=order.broker_order_id,
            status=ReconciliationStatus.SKIPPED_ALREADY_RESOLVED,
            broker_status=status.raw_status,
            detail=(
                "another process resolved this order between reading it and writing it; "
                "no second update and no second fill were applied."
            ),
        )

    if not status.executed:
        return ReconciliationOutcome(
            order_id=order.id,
            broker_order_id=order.broker_order_id,
            status=ReconciliationStatus.RESOLVED_CLOSED_UNFILLED,
            broker_status=status.raw_status,
        )

    # Both are non-None whenever `status.executed` is true - that is what
    # `LiveOrderStatus.executed` asserts - so this fill is built entirely
    # from figures the broker itself reported.
    assert status.executed_quantity is not None
    assert status.executed_price is not None
    session.add(
        FillRow(
            order_id=order.id,
            quantity=status.executed_quantity,
            fill_price=status.executed_price,
            # The venue's own execution timestamp when it gave one. Falling
            # back to the reconciliation instant is honest about what it is:
            # the earliest moment this system can prove the fill existed. It
            # is never fabricated from `submitted_at`, which would claim the
            # fill happened at a time nobody reported.
            filled_at=status.updated_at or now,
        )
    )

    return ReconciliationOutcome(
        order_id=order.id,
        broker_order_id=order.broker_order_id,
        status=ReconciliationStatus.RESOLVED_FILLED,
        broker_status=status.raw_status,
    )


async def run_reconciliation_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    live_broker: LiveBrokerAdapter | None,
    cycle_lock: SnapshotCycleLock | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> ReconciliationCycleResult:
    """One full pass: resolve every unresolved live order, independently.

    ORDER OF OPERATIONS, AND WHY
    ----------------------------
    1. The NOT_CONFIGURED short-circuit comes FIRST, before the lock and
       before any SQL. On a default deployment (`LIVE_TRADING_ENABLED=false`)
       `live_broker` is None, so an enabled-but-unconfigured reconciler
       costs exactly one log line per interval - no database round-trip, no
       advisory lock, no connection. This mirrors D042 putting the
       market-hours gate ahead of the snapshot cycle's broker query for the
       same reason.
    2. Then the cross-worker advisory lock (D047), on a session of its own
       held open for the whole pass.
    3. Then one query for the work, then one session PER ORDER.

    Each order gets its own session and its own commit so that one order's
    failed write cannot poison another's transaction, and so a slow cycle
    does not hold one connection open across every broker round-trip.
    Orders are processed sequentially, not concurrently: they are calls to a
    single rate-limited trading venue, and the adapter wraps a synchronous
    SDK client (D058) - fanning out would buy nothing and risk throttling
    the one API this system must never lose access to mid-position.
    """
    if live_broker is None:
        detail = (
            "NOT_CONFIGURED: no live execution path is available, so there is no broker "
            "to ask about any order. Live trading requires TRADING_MODE=live, "
            "LIVE_TRADING_ENABLED=true, and all three LONGPORT_LIVE_* credentials set "
            "together (D058). The cycle was skipped without querying the database."
        )
        logger.info("live_order_reconciler_cycle_skipped_not_configured", reason=detail)
        return ReconciliationCycleResult(
            outcomes=(),
            status=ReconciliationCycleStatus.SKIPPED_DISABLED,
            detail=detail,
        )

    lock = cycle_lock if cycle_lock is not None else SnapshotCycleLock(enabled=False)

    # A session of its own, opened solely to own the advisory lock and held
    # for the whole pass - see run_snapshot_cycle's identical note. It is
    # never committed, because a session-level advisory lock belongs to the
    # backend connection and returning that connection to the pool would
    # hand the lock away mid-cycle.
    async with session_factory() as lock_session, lock.hold(lock_session) as lock_decision:
        if not lock_decision.should_run:
            return ReconciliationCycleResult(
                outcomes=(),
                status=ReconciliationCycleStatus.SKIPPED_LOCK_HELD,
                lock=lock_decision,
            )

        async with session_factory() as session:
            orders = await unresolved_live_orders(session)

        outcomes: list[ReconciliationOutcome] = []
        for order in orders:
            broker_order_id = order.broker_order_id
            assert broker_order_id is not None  # enforced in SQL above

            try:
                status = live_broker.get_order_status(broker_order_id)
            except LiveBrokerError as exc:
                # A failure to OBSERVE is not evidence about the thing
                # observed. Skip this order, change nothing, try next cycle.
                outcome = ReconciliationOutcome(
                    order_id=order.id,
                    broker_order_id=broker_order_id,
                    status=ReconciliationStatus.SKIPPED_BROKER_UNAVAILABLE,
                    detail=str(exc),
                )
                outcomes.append(outcome)
                logger.warning(
                    "live_order_reconciliation_skipped",
                    order_id=str(order.id),
                    broker_order_id=broker_order_id,
                    status=outcome.status.value,
                    detail=outcome.detail,
                )
                continue

            async with session_factory() as session:
                outcome = await resolve_order(session, order, status, now=clock())
                if outcome.resolved:
                    await session.commit()
                else:
                    # STILL_OPEN and SKIPPED_ALREADY_RESOLVED both wrote
                    # nothing worth keeping; roll back explicitly rather
                    # than relying on the context manager, so the intent is
                    # visible at the point it is decided.
                    await session.rollback()

            outcomes.append(outcome)

            if outcome.resolved:
                # info, not warning: this is the job succeeding. It is still
                # logged for every single order, because each line is the
                # record of a real position changing hands.
                logger.info(
                    "live_order_reconciled",
                    order_id=str(outcome.order_id),
                    broker_order_id=outcome.broker_order_id,
                    status=outcome.status.value,
                    broker_status=outcome.broker_status,
                )
            elif outcome.status is ReconciliationStatus.STILL_OPEN:
                logger.info(
                    "live_order_still_open",
                    order_id=str(outcome.order_id),
                    broker_order_id=outcome.broker_order_id,
                    broker_status=outcome.broker_status,
                )
            else:
                logger.warning(
                    "live_order_reconciliation_skipped",
                    order_id=str(outcome.order_id),
                    broker_order_id=outcome.broker_order_id,
                    status=outcome.status.value,
                    detail=outcome.detail,
                )

        return ReconciliationCycleResult(
            outcomes=tuple(outcomes),
            status=ReconciliationCycleStatus.RAN,
            lock=lock_decision,
        )


class LiveOrderReconciler:
    """In-process periodic reconciliation task, owned by the FastAPI
    lifespan - a sibling of `PortfolioSnapshotScheduler`, not a replacement
    for it. The two run independently, on their own intervals and their own
    advisory-lock keys.

    Runs one cycle immediately on `start()`, then every `interval_seconds`.
    Running immediately is deliberate for the same reason D030 gave: it
    makes a misconfiguration visible in the startup logs instead of an
    interval later, and it means a restart resolves anything left
    outstanding by the process that died rather than waiting.

    The loop never dies. Any unexpected exception from a cycle is logged and
    the loop sleeps and tries again - a reconciler that stopped permanently
    on one transient blip would silently leave real orders unresolved
    forever, which is a strictly worse failure than a logged, retried error.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        live_broker: LiveBrokerAdapter | None,
        interval_seconds: int,
        cycle_lock: SnapshotCycleLock | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError(
                "interval_seconds must be positive; a zero or negative interval "
                "would busy-loop the reconciliation cycle against a real broker's API."
            )
        self._session_factory = session_factory
        self._live_broker = live_broker
        self._interval_seconds = interval_seconds
        # None means "no cross-worker locking", matching how
        # PortfolioSnapshotScheduler treats an omitted lock. The app wires a
        # real one in main.py.
        self._cycle_lock = (
            cycle_lock if cycle_lock is not None else SnapshotCycleLock(enabled=False)
        )
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("LiveOrderReconciler.start() called twice")
        self._task = asyncio.create_task(self._run(), name="live-order-reconciler")
        logger.info(
            "live_order_reconciler_started",
            interval_seconds=self._interval_seconds,
            # "configured" here means a live TradeContext exists. It does
            # NOT mean any order will be resolved - only that there is
            # somebody to ask. NOT_CONFIGURED means every cycle will
            # short-circuit, which is the committed default.
            live_broker="longbridge" if self._live_broker else "NOT_CONFIGURED",
            cycle_lock=(
                f"pg_advisory:{self._cycle_lock.classid}/{self._cycle_lock.objid}"
                if self._cycle_lock.enabled
                else "DISABLED"
            ),
        )

    async def stop(self) -> None:
        """Cancels the loop and waits for it to actually finish, so shutdown
        never races a half-committed reconciliation."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
        logger.info("live_order_reconciler_stopped")

    async def run_once(self) -> ReconciliationCycleResult:
        """One cycle, awaited directly. Used by the loop below and by tests
        that need a deterministic single pass rather than wall-clock
        timing."""
        return await run_reconciliation_cycle(
            self._session_factory,
            live_broker=self._live_broker,
            cycle_lock=self._cycle_lock,
            clock=self._clock,
        )

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - see class docstring
                logger.error(
                    "live_order_reconciliation_cycle_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            await asyncio.sleep(self._interval_seconds)
