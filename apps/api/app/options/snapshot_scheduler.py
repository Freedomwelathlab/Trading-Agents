"""The daily option-chain snapshot loop (Phase 100, D119).

A sibling of the portfolio snapshot scheduler and the other lifespan-owned
loops, with the same shape: an asyncio task that wakes every interval,
takes the shared Postgres advisory-lock class on its OWN object key so N
API workers produce one snapshot rather than N, and never dies on a
transient error.

**When it captures.** On a New York weekday, once the New York clock is
past `capture_after` (default 16:30 ET), for each configured underlying
that has no snapshot for today captured at or after that cutoff. So:

* one capture per trading day, after the close - the rest of the wakes
  are cheap no-ops that read one aggregate;
* an operator's intraday on-demand capture does NOT satisfy the day
  (it was taken before the cutoff) and is replaced by the after-close one;
* a restart after the capture does not capture again.

**Why New York time and not "20:30 UTC".** 20:30 UTC is 16:30 EDT but
15:30 EST: for the whole winter a UTC schedule would record the chain half
an hour BEFORE the close and call it the day's snapshot.

**Holidays are not modelled - the vendor is asked.** The capture requires
the vendor's own `as_of` to be today's New York date; on a market holiday
the feed still shows the previous session, the capture returns
`stale_source`, and nothing is written. A weekday-only gate plus that check
is enough, and it cannot be wrong about a holiday the way a hard-coded
calendar can.

Default OFF (`OPTION_SNAPSHOT_SCHEDULER_ENABLED=false`): the loop writes
rows forever to a metered database, so turning it on is the operator's
decision. It never places, sizes or prices a trade.
"""

from __future__ import annotations

import asyncio
import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app.core.logging import get_logger
from apps.api.app.marketdata.option_chain_provider import OptionChainProvider
from apps.api.app.options.snapshots import (
    DEFAULT_MAX_DTE,
    DEFAULT_STRIKE_BAND,
    NEW_YORK,
    CaptureStatus,
    capture_chain_snapshot,
    latest_captured_at,
)
from apps.api.app.portfolio.cycle_lock import SnapshotCycleLock, SnapshotCycleLockDecision

logger = get_logger(__name__)

OPTION_SNAPSHOT_LOCK_OBJID = 1869640819
"""ASCII ``b"opts"`` big-endian (0x6F707473): this job's own advisory-lock
object key, in the same classid namespace as the other background jobs, so
it neither collides with nor excludes them."""


def build_option_snapshot_cycle_lock(*, enabled: bool = True) -> SnapshotCycleLock:
    return SnapshotCycleLock(
        enabled=enabled, objid=OPTION_SNAPSHOT_LOCK_OBJID, job_name="option_snapshot"
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


class OptionSnapshotOutcome(str, enum.Enum):  # noqa: UP042
    CAPTURED = "captured"
    SKIPPED_WEEKEND = "skipped_weekend"
    SKIPPED_NOT_YET = "skipped_not_yet"
    SKIPPED_ALREADY_CAPTURED = "skipped_already_captured"
    SKIPPED_STALE_SOURCE = "skipped_stale_source"
    SKIPPED_LOCK_HELD = "skipped_lock_held"
    SKIPPED_NOT_CONFIGURED = "skipped_not_configured"
    FAILED = "failed"


@dataclass(frozen=True)
class UnderlyingOutcome:
    underlying: str
    outcome: OptionSnapshotOutcome
    rows_written: int = 0
    detail: str | None = None


@dataclass(frozen=True)
class OptionSnapshotCycleResult:
    outcome: OptionSnapshotOutcome
    per_underlying: tuple[UnderlyingOutcome, ...] = field(default_factory=tuple)


def capture_due(now: datetime, capture_after: time) -> OptionSnapshotOutcome | None:
    """None when a capture is due by the clock; otherwise why not.

    Pure, so the weekend and cut-off rules are tested without a loop."""
    local = now.astimezone(NEW_YORK)
    if local.weekday() >= 5:
        return OptionSnapshotOutcome.SKIPPED_WEEKEND
    if local.time() < capture_after:
        return OptionSnapshotOutcome.SKIPPED_NOT_YET
    return None


def cutoff_instant(now: datetime, capture_after: time) -> datetime:
    """Today's New York `capture_after`, as an aware instant."""
    local = now.astimezone(NEW_YORK)
    return datetime.combine(local.date(), capture_after, tzinfo=NEW_YORK)


async def run_option_snapshot_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider: OptionChainProvider | None,
    underlyings: list[str],
    capture_after: time,
    max_dte: int = DEFAULT_MAX_DTE,
    strike_band: Decimal = DEFAULT_STRIKE_BAND,
    cycle_lock: SnapshotCycleLock | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> OptionSnapshotCycleResult:
    """One wake of the loop. Each underlying is its own transaction, so a
    vendor failure on one does not discard another's snapshot."""
    now = clock()
    if provider is None:
        return OptionSnapshotCycleResult(OptionSnapshotOutcome.SKIPPED_NOT_CONFIGURED)
    not_due = capture_due(now, capture_after)
    if not_due is not None:
        return OptionSnapshotCycleResult(not_due)

    lock = cycle_lock if cycle_lock is not None else SnapshotCycleLock(enabled=False)
    today = now.astimezone(NEW_YORK).date()
    cutoff = cutoff_instant(now, capture_after)

    async with session_factory() as lock_session, lock.hold(lock_session) as decision:
        if decision is SnapshotCycleLockDecision.SKIPPED_LOCK_HELD:
            return OptionSnapshotCycleResult(OptionSnapshotOutcome.SKIPPED_LOCK_HELD)

        results: list[UnderlyingOutcome] = []
        for underlying in underlyings:
            async with session_factory() as session:
                try:
                    taken = await latest_captured_at(session, underlying, today)
                    if taken is not None and taken >= cutoff:
                        results.append(
                            UnderlyingOutcome(
                                underlying, OptionSnapshotOutcome.SKIPPED_ALREADY_CAPTURED
                            )
                        )
                        continue
                    result = await capture_chain_snapshot(
                        session,
                        provider,
                        underlying,
                        max_dte=max_dte,
                        strike_band=strike_band,
                        now=now,
                        require_trade_date=today,
                    )
                    if result.status is CaptureStatus.STALE_SOURCE:
                        results.append(
                            UnderlyingOutcome(
                                underlying,
                                OptionSnapshotOutcome.SKIPPED_STALE_SOURCE,
                                detail=f"vendor quotes are for {result.trade_date.isoformat()}",
                            )
                        )
                        continue
                    await session.commit()
                    results.append(
                        UnderlyingOutcome(
                            underlying,
                            OptionSnapshotOutcome.CAPTURED,
                            rows_written=result.rows_written,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - one symbol must not stop the rest
                    await session.rollback()
                    logger.error(
                        "option_snapshot_capture_failed",
                        underlying=underlying,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    results.append(
                        UnderlyingOutcome(
                            underlying,
                            OptionSnapshotOutcome.FAILED,
                            detail=f"{type(exc).__name__}: {exc}",
                        )
                    )
        return OptionSnapshotCycleResult(_overall(results), tuple(results))


_OVERALL_PRIORITY = (
    OptionSnapshotOutcome.CAPTURED,
    OptionSnapshotOutcome.FAILED,
    OptionSnapshotOutcome.SKIPPED_STALE_SOURCE,
    OptionSnapshotOutcome.SKIPPED_ALREADY_CAPTURED,
)


def _overall(results: list[UnderlyingOutcome]) -> OptionSnapshotOutcome:
    """The cycle's one-word summary: the most consequential per-symbol
    outcome (a capture beats a failure beats a stale feed beats a no-op).
    The per-symbol list is always returned alongside it."""
    present = {r.outcome for r in results}
    for outcome in _OVERALL_PRIORITY:
        if outcome in present:
            return outcome
    return OptionSnapshotOutcome.SKIPPED_ALREADY_CAPTURED


class OptionSnapshotScheduler:
    """Periodic loop; one cycle immediately on `start()`, then every
    `interval_seconds`. Never dies on a transient error."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        provider: OptionChainProvider | None,
        underlyings: list[str],
        capture_after: time,
        interval_seconds: int,
        max_dte: int = DEFAULT_MAX_DTE,
        strike_band: Decimal = DEFAULT_STRIKE_BAND,
        cycle_lock: SnapshotCycleLock | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive.")
        self._session_factory = session_factory
        self._provider = provider
        self._underlyings = list(underlyings)
        self._capture_after = capture_after
        self._interval_seconds = interval_seconds
        self._max_dte = max_dte
        self._strike_band = strike_band
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
            raise RuntimeError("OptionSnapshotScheduler.start() called twice")
        self._task = asyncio.create_task(self._run(), name="option-snapshot-scheduler")
        logger.info(
            "option_snapshot_scheduler_started",
            interval_seconds=self._interval_seconds,
            underlyings=self._underlyings,
            capture_after_et=self._capture_after.isoformat(timespec="minutes"),
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
        logger.info("option_snapshot_scheduler_stopped")

    async def run_once(self) -> OptionSnapshotCycleResult:
        return await run_option_snapshot_cycle(
            self._session_factory,
            provider=self._provider,
            underlyings=self._underlyings,
            capture_after=self._capture_after,
            max_dte=self._max_dte,
            strike_band=self._strike_band,
            cycle_lock=self._cycle_lock,
            clock=self._clock,
        )

    async def _run(self) -> None:
        while True:
            try:
                result = await self.run_once()
                logger.info(
                    "option_snapshot_cycle",
                    outcome=result.outcome.value,
                    per_underlying=[
                        f"{r.underlying}:{r.outcome.value}:{r.rows_written}"
                        for r in result.per_underlying
                    ],
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - see class docstring
                logger.error(
                    "option_snapshot_cycle_failed", error=str(exc), error_type=type(exc).__name__
                )
            await asyncio.sleep(self._interval_seconds)
