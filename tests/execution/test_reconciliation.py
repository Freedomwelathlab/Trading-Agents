"""Unit tests for the live-order reconciler's contract (Phase 49, D066) -
the parts provable without a database: how a broker status string is
classified, that the adapter's status lookup reads the real SDK shape, and
that the cycle short-circuits before doing anything when no live path
exists.

NO REAL BROKER IS EVER CONTACTED AND LIVE TRADING IS NEVER ENABLED. Every
adapter here is the production `LiveBrokerAdapter` driven by a fake client
with no network access, exactly as tests/execution/test_live_broker.py
established in Phase 43. No `Settings` object anywhere in this file has
`live_trading_enabled` set, and `build_live_broker_adapter()` is never
called.

The half that needs a real database - that a cycle actually mutates the
right rows, writes a real fill, and excludes another worker - is in
tests/api/test_live_order_reconciler.py against real Postgres. Nothing here
claims to have proven any of that.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.execution.live_broker import (
    LiveBrokerAdapter,
    LiveBrokerError,
    LiveOrderStatus,
)
from apps.api.app.execution.reconciliation import (
    LiveOrderReconciler,
    ReconciliationCycleStatus,
    ReconciliationStatus,
    build_reconciler_cycle_lock,
    run_reconciliation_cycle,
)
from apps.api.app.portfolio.cycle_lock import (
    RECONCILER_LOCK_OBJID,
    SNAPSHOT_LOCK_CLASSID,
    SNAPSHOT_LOCK_OBJID,
)
from tests.execution.test_live_broker import FakeLiveTradeClient, _OrderDetail


class MappedLiveTradeClient(FakeLiveTradeClient):
    """A fake whose `order_detail` answers PER ORDER ID, so one cycle can
    contain a filled order, a still-working order and an unreachable one at
    the same time - which is the situation the reconciler actually has to
    handle and the one a single-response fake cannot express.

    Extends the Phase 43 fake rather than replacing it so the two files
    cannot drift apart on what a Longbridge response looks like.
    """

    def __init__(
        self,
        details: dict[str, _OrderDetail] | None = None,
        *,
        raises: dict[str, Exception] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._details = details or {}
        self._raises = raises or {}
        self.status_calls: list[str] = []

    def order_detail(self, order_id: str) -> _OrderDetail:
        self.status_calls.append(order_id)
        if order_id in self._raises:
            raise self._raises[order_id]
        if order_id in self._details:
            return self._details[order_id]
        raise KeyError(f"the test did not stub order {order_id!r}")


def _adapter(**kwargs: object) -> LiveBrokerAdapter:
    return LiveBrokerAdapter(MappedLiveTradeClient(**kwargs))  # type: ignore[arg-type]


# --- how a broker status string is classified ----------------------------


def test_a_filled_order_with_real_figures_counts_as_executed():
    status = LiveOrderStatus(
        order_id="LB-1",
        raw_status="Filled",
        executed_quantity=Decimal("10"),
        executed_price=Decimal("101.25"),
    )
    assert status.terminal is True
    assert status.executed is True
    assert status.closed_unfilled is False
    assert status.still_open is False


@pytest.mark.parametrize("raw", ["Canceled", "Expired", "Rejected", "PartialWithdrawal"])
def test_terminal_statuses_with_no_execution_are_closed_unfilled(raw):
    """These are the four the SDK actually defines for "finished, nothing
    executed" - verified by introspecting `longport.openapi.OrderStatus`,
    not from documentation. Note the SDK's spelling is `Canceled`."""
    status = LiveOrderStatus(order_id="LB-1", raw_status=raw)
    assert status.terminal is True
    assert status.closed_unfilled is True
    assert status.executed is False
    assert status.still_open is False


@pytest.mark.parametrize(
    "raw",
    [
        "New",
        "WaitToNew",
        "PartialFilled",
        "PendingCancel",
        "Replaced",
        "Unknown",
        "SomeFutureStatus",
    ],
)
def test_every_unclassified_status_is_treated_as_still_open(raw):
    """The fail-closed direction, and the single most important assertion in
    this file. Leaving an order under observation costs one API call next
    cycle; declaring it finished on a status this code does not understand
    would permanently record an outcome the broker never reported.

    `PartialFilled` is in this list on purpose: it is a real execution but
    NOT a finished order, and freezing a half-done quantity into an
    append-only audit trail would be a number that was true for a moment and
    wrong forever after."""
    status = LiveOrderStatus(
        order_id="LB-1",
        raw_status=raw,
        executed_quantity=Decimal("5"),
        executed_price=Decimal("100"),
    )
    assert status.still_open is True
    assert status.terminal is False
    assert status.executed is False
    assert status.closed_unfilled is False


def test_a_terminal_status_without_a_price_is_not_a_fill():
    """No-fabrication, at the narrowest point it applies: a fill is a claim
    about a price, so a response with no price cannot be completed into
    one."""
    no_price = LiveOrderStatus(
        order_id="LB-1", raw_status="Filled", executed_quantity=Decimal("10")
    )
    assert no_price.executed is False
    assert no_price.closed_unfilled is True

    no_quantity = LiveOrderStatus(
        order_id="LB-1", raw_status="Filled", executed_price=Decimal("100")
    )
    assert no_quantity.executed is False

    zero_quantity = LiveOrderStatus(
        order_id="LB-1",
        raw_status="Filled",
        executed_quantity=Decimal("0"),
        executed_price=Decimal("100"),
    )
    assert zero_quantity.executed is False


# --- the adapter's real status lookup ------------------------------------


def test_get_order_status_reads_the_real_sdk_response_shape():
    """Reads through the production `LiveBrokerAdapter` against the same
    fake response shape Phase 43's tests use, so the mapping from a
    Longbridge `order_detail` onto this system's types is exercised rather
    than assumed."""
    when = datetime(2026, 9, 3, 14, 30, tzinfo=UTC)
    adapter = _adapter(
        details={
            "LB-7": _OrderDetail(
                status="Filled",
                executed_quantity="10",
                executed_price="99.5",
                updated_at=when,
            )
        }
    )

    status = adapter.get_order_status("LB-7")

    assert status.order_id == "LB-7"
    assert status.raw_status == "Filled"
    assert status.executed_quantity == Decimal("10")
    assert status.executed_price == Decimal("99.5")
    assert status.updated_at == when
    assert status.executed is True


def test_get_order_status_stamps_utc_on_a_naive_broker_timestamp():
    """A naive datetime compared against this system's tz-aware ones would
    raise at the worst possible moment. Longbridge reports UTC."""
    adapter = _adapter(
        details={
            "LB-7": _OrderDetail(
                status="Filled",
                executed_quantity="10",
                executed_price="99.5",
                updated_at=datetime(2026, 9, 3, 14, 30),
            )
        }
    )

    status = adapter.get_order_status("LB-7")
    assert status.updated_at is not None
    assert status.updated_at.tzinfo is UTC


def test_get_order_status_raises_rather_than_returning_a_degraded_status():
    """A failure to observe must never be reported as an observation. The
    caller's only correct response is to leave the order alone."""
    adapter = _adapter(raises={"LB-7": RuntimeError("connection reset")})

    with pytest.raises(LiveBrokerError, match="LB-7"):
        adapter.get_order_status("LB-7")


def test_an_unparseable_execution_figure_is_an_error_not_a_zero():
    """Coercing garbage into a number would write a permanent fill row from
    a response this code did not understand."""
    adapter = _adapter(
        details={
            "LB-7": _OrderDetail(status="Filled", executed_quantity=None, executed_price=None)
        }
    )
    # None is legitimate ("not reported") and must NOT raise - it simply
    # means there is nothing to record.
    status = adapter.get_order_status("LB-7")
    assert status.executed_quantity is None
    assert status.executed is False


# --- the disabled / not-configured short-circuit --------------------------


@pytest.mark.asyncio
async def test_a_cycle_with_no_live_broker_touches_nothing_at_all():
    """The NOT_CONFIGURED discipline, and the property that makes enabling
    this job safe on a default deployment. `session_factory` here is a
    callable that EXPLODES if called - so the assertion is not "it returned
    a skip" but "it never reached the database at all"."""

    def exploding_factory():  # pragma: no cover - calling it is the failure
        raise AssertionError(
            "a NOT_CONFIGURED reconciliation cycle must not open a database session"
        )

    result = await run_reconciliation_cycle(
        exploding_factory,  # type: ignore[arg-type]
        live_broker=None,
        cycle_lock=build_reconciler_cycle_lock(),
    )

    assert result.status is ReconciliationCycleStatus.SKIPPED_DISABLED
    assert result.ran is False
    assert result.outcomes == ()
    assert result.detail is not None
    assert "NOT_CONFIGURED" in result.detail


@pytest.mark.asyncio
async def test_the_reconciler_loop_also_short_circuits_when_unconfigured():
    """The wiring, not just the function: main.py constructs the reconciler
    with whatever `build_live_broker_adapter()` returned, which on the
    committed defaults is None."""

    def exploding_factory():  # pragma: no cover
        raise AssertionError("must not open a session")

    reconciler = LiveOrderReconciler(
        exploding_factory,  # type: ignore[arg-type]
        live_broker=None,
        interval_seconds=300,
    )
    result = await reconciler.run_once()

    assert result.status is ReconciliationCycleStatus.SKIPPED_DISABLED
    assert reconciler.running is False


def test_a_non_positive_interval_is_refused_at_construction():
    """A zero interval would busy-loop a real trading venue's API, which is
    how credentials get throttled or revoked."""
    for bad in (0, -1):
        with pytest.raises(ValueError, match="must be positive"):
            LiveOrderReconciler(
                lambda: None,  # type: ignore[arg-type,return-value]
                live_broker=None,
                interval_seconds=bad,
            )


# --- the lock this job takes ---------------------------------------------


def test_the_reconciler_lock_shares_the_namespace_but_not_the_object_key():
    """The whole point of D047's two-key form. Sharing the classid keeps
    both jobs greppable in `pg_locks` under one namespace; NOT sharing the
    objid is what lets a reconciliation cycle and a snapshot cycle run at
    the same time. If these two objids were ever equal, every reconciler
    cycle would silently skip whenever a snapshot was in flight - a failure
    that looks exactly like "there was nothing to do"."""
    lock = build_reconciler_cycle_lock()

    assert lock.classid == SNAPSHOT_LOCK_CLASSID
    assert lock.objid == RECONCILER_LOCK_OBJID
    assert lock.objid != SNAPSHOT_LOCK_OBJID
    assert RECONCILER_LOCK_OBJID == int.from_bytes(b"recn", "big") == 1919247214
    # Must fit a signed int4 or Postgres rejects the two-key form.
    assert 0 < RECONCILER_LOCK_OBJID <= 2**31 - 1


def test_the_reconciler_lock_logs_under_its_own_job_name():
    """Otherwise a reconciler's contention would appear in the logs as the
    snapshot scheduler's, and an operator chasing one would be reading the
    other's evidence."""
    assert build_reconciler_cycle_lock().job_name == "live_order_reconciler"


def test_the_reconciler_lock_can_be_switched_off():
    assert build_reconciler_cycle_lock(enabled=False).enabled is False
    assert build_reconciler_cycle_lock().enabled is True


def test_the_snapshot_lock_keeps_its_pre_phase_49_log_names():
    """`job_name` was added with a default that reproduces D047's event
    names byte-for-byte, so no existing log query breaks."""
    from apps.api.app.portfolio.cycle_lock import SnapshotCycleLock

    assert SnapshotCycleLock().job_name == "portfolio_snapshot"
    assert f"{SnapshotCycleLock().job_name}_cycle_lock_not_acquired" == (
        "portfolio_snapshot_cycle_lock_not_acquired"
    )


# --- the outcome types ----------------------------------------------------


def test_only_the_two_resolved_statuses_count_as_resolved():
    """"Still open" and "the broker was unreachable" must never be
    conflated with "we know what happened" - they are the two states in
    which nothing at all was written."""
    from apps.api.app.execution.reconciliation import ReconciliationOutcome

    def outcome(status):
        return ReconciliationOutcome(
            order_id=uuid.uuid4(), broker_order_id="LB-1", status=status
        )

    assert outcome(ReconciliationStatus.RESOLVED_FILLED).resolved is True
    assert outcome(ReconciliationStatus.RESOLVED_CLOSED_UNFILLED).resolved is True
    assert outcome(ReconciliationStatus.STILL_OPEN).resolved is False
    assert outcome(ReconciliationStatus.SKIPPED_BROKER_UNAVAILABLE).resolved is False
    assert outcome(ReconciliationStatus.SKIPPED_ALREADY_RESOLVED).resolved is False
