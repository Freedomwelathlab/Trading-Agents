"""The Strategy Library bot runner (Phase 109, D136).

A sibling of the options-bot runner with the same safety shape: only
ACTIVE bots, one transaction per bot, always a terminal run row, and
cross-worker exclusion through a Postgres advisory lock on this job's own
key.

**Slow by design.** These are daily strategies: a decision changes at most
once a session. The loop wakes every `LIBRARY_BOT_RUNNER_INTERVAL_SECONDS`
(default 15 minutes), and a bot whose last cycle found nothing to do is
not given another run row for an hour - so an idle bot costs about one
small row an hour rather than one a minute. History comes from the
library's in-process cache, so most wake-ups make no vendor call at all
beyond a quote per held symbol.

DEFAULT: OFF (`LIBRARY_BOT_RUNNER_ENABLED=false`). "Run now" on an approved
bot works with the loop off; the switch gates the unattended loop only.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app.core.config import Settings
from apps.api.app.core.logging import get_logger
from apps.api.app.db.models import (
    LibraryBot,
    LibraryBotRun,
    LibraryBotRunStatus,
    LibraryBotStatus,
)
from apps.api.app.library.bot import LibraryBotOutcome, QuoteProvider, run_library_bot_cycle
from apps.api.app.library.service import DailyHistoryProvider
from apps.api.app.notifications.alerts import AlertSink, notify
from apps.api.app.portfolio.cycle_lock import SnapshotCycleLock, SnapshotCycleLockDecision

logger = get_logger(__name__)

IDLE_ROW_INTERVAL = timedelta(hours=1)
"""After a cycle that filled nothing, the scheduled loop leaves the bot
alone this long. A new daily decision cannot arrive faster than that."""

LIBRARY_BOT_RUNNER_LOCK_OBJID = 1818390388
"""ASCII ``b"lbot"`` big-endian (0x6c626f74) - this job's own advisory-lock
object key, same classid namespace as the other background jobs."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def build_library_bot_runner_cycle_lock(*, enabled: bool = True) -> SnapshotCycleLock:
    return SnapshotCycleLock(
        enabled=enabled, objid=LIBRARY_BOT_RUNNER_LOCK_OBJID, job_name="library_bot_runner"
    )


class LibraryBotCycleStatus(str, Enum):  # noqa: UP042
    RAN = "ran"
    SKIPPED_LOCK_HELD = "skipped_lock_held"


@dataclass
class LibraryBotCycleResult:
    status: LibraryBotCycleStatus
    outcomes: tuple[LibraryBotOutcome, ...] = field(default_factory=tuple)


def library_bot_alert_text(
    name: str, outcome: LibraryBotOutcome, paused_reason: str | None
) -> str | None:
    """The one-line alert for a cycle, or None when nothing happened that
    an operator needs to hear about. An idle cycle is silent on purpose:
    an alert every fifteen minutes is an alert nobody reads."""
    label = f'Library bot "{name}" (paper)'
    if outcome.status is LibraryBotRunStatus.FAILED:
        return f"{label}: cycle FAILED - {outcome.detail or 'no detail'}"
    if paused_reason:
        return f"{label}: PAUSED - {paused_reason}"
    if outcome.orders_filled or outcome.orders_rejected:
        equity = f" Equity {outcome.equity:,.2f}." if outcome.equity is not None else ""
        return (
            f"{label}: {outcome.orders_filled} filled, {outcome.orders_rejected} rejected."
            f"{equity} {outcome.detail or ''}".rstrip()
        )
    return None


async def _recently_idle(session: AsyncSession, bot_id: uuid.UUID, now: datetime) -> bool:
    latest = (
        await session.execute(
            select(LibraryBotRun)
            .where(LibraryBotRun.bot_id == bot_id)
            .order_by(LibraryBotRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return (
        latest is not None
        and latest.orders_filled == 0
        and now - latest.started_at < IDLE_ROW_INTERVAL
    )


async def run_library_bot_isolated(
    session_factory: async_sessionmaker[AsyncSession],
    bot_id: uuid.UUID,
    *,
    settings: Settings,
    clock: Callable[[], datetime],
    provider: DailyHistoryProvider | None,
    quotes: QuoteProvider | None,
    throttle_idle: bool = True,
    alerts: AlertSink | None = None,
) -> LibraryBotOutcome | None:
    started = clock()
    async with session_factory() as session:
        bot = await session.get(LibraryBot, bot_id)
        if bot is None:
            return None
        if throttle_idle and await _recently_idle(session, bot.id, started):
            return None
        bot_name = bot.name
        was_active = bot.status is LibraryBotStatus.ACTIVE
        run = LibraryBotRun(
            id=uuid.uuid4(),
            bot_id=bot.id,
            status=LibraryBotRunStatus.FAILED,  # provisional; resolved below
            started_at=started,
        )
        session.add(run)
        await session.flush()
        try:
            outcome = await run_library_bot_cycle(
                session,
                bot,
                run,
                settings=settings,
                clock=clock,
                provider=provider,
                quotes=quotes,
            )
            # Only a pause that happened IN this cycle is news.
            paused_reason = (
                bot.paused_reason
                if was_active and bot.status is LibraryBotStatus.PAUSED
                else None
            )
            await session.commit()
            # After the commit, so an alert never describes a cycle that
            # was rolled back; best-effort, so it never fails one either.
            text = library_bot_alert_text(bot_name, outcome, paused_reason)
            if text is not None:
                await notify(alerts, text)
            return outcome
        except Exception as exc:  # noqa: BLE001 - the run row must resolve
            await session.rollback()
            async with session_factory() as fresh:
                fresh.add(
                    LibraryBotRun(
                        id=run.id,
                        bot_id=bot_id,
                        status=LibraryBotRunStatus.FAILED,
                        started_at=started,
                        completed_at=clock(),
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
                await fresh.commit()
            logger.error(
                "library_bot_cycle_failed",
                bot_id=str(bot_id),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            failed = LibraryBotOutcome(
                bot_id=bot_id,
                run_id=run.id,
                status=LibraryBotRunStatus.FAILED,
                detail=f"{type(exc).__name__}: {exc}",
            )
            text = library_bot_alert_text(bot_name, failed, None)
            if text is not None:
                await notify(alerts, text)
            return failed


async def run_library_bot_cycle_all(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    provider: DailyHistoryProvider | None,
    quotes: QuoteProvider | None,
    cycle_lock: SnapshotCycleLock | None = None,
    clock: Callable[[], datetime] = utc_now,
    only_bot_id: uuid.UUID | None = None,
    alerts: AlertSink | None = None,
) -> LibraryBotCycleResult:
    """One pass over every ACTIVE bot (or one bot, for "run now")."""
    lock = cycle_lock if cycle_lock is not None else SnapshotCycleLock(enabled=False)
    async with session_factory() as lock_session, lock.hold(lock_session) as decision:
        if decision is SnapshotCycleLockDecision.SKIPPED_LOCK_HELD:
            return LibraryBotCycleResult(status=LibraryBotCycleStatus.SKIPPED_LOCK_HELD)
        async with session_factory() as session:
            query = select(LibraryBot.id)
            if only_bot_id is not None:
                query = query.where(LibraryBot.id == only_bot_id)
            else:
                query = query.where(LibraryBot.status == LibraryBotStatus.ACTIVE)
            bot_ids = list((await session.execute(query)).scalars().all())
        outcomes: list[LibraryBotOutcome] = []
        for bot_id in bot_ids:
            outcome = await run_library_bot_isolated(
                session_factory,
                bot_id,
                settings=settings,
                clock=clock,
                provider=provider,
                quotes=quotes,
                throttle_idle=only_bot_id is None,
                alerts=alerts,
            )
            if outcome is not None:
                outcomes.append(outcome)
        return LibraryBotCycleResult(status=LibraryBotCycleStatus.RAN, outcomes=tuple(outcomes))


class LibraryBotRunner:
    """Periodic loop. Never dies on a transient error."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        interval_seconds: int,
        provider: DailyHistoryProvider | None,
        quotes: QuoteProvider | None,
        cycle_lock: SnapshotCycleLock | None = None,
        clock: Callable[[], datetime] = utc_now,
        alerts: AlertSink | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive.")
        self._session_factory = session_factory
        self._settings = settings
        self._interval_seconds = interval_seconds
        self._provider = provider
        self._quotes = quotes
        self._cycle_lock = (
            cycle_lock if cycle_lock is not None else SnapshotCycleLock(enabled=False)
        )
        self._clock = clock
        self._alerts = alerts
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("LibraryBotRunner.start() called twice")
        self._task = asyncio.create_task(self._run(), name="library-bot-runner")
        logger.info("library_bot_runner_started", interval_seconds=self._interval_seconds)

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
        logger.info("library_bot_runner_stopped")

    async def run_once(self, *, only_bot_id: uuid.UUID | None = None) -> LibraryBotCycleResult:
        return await run_library_bot_cycle_all(
            self._session_factory,
            settings=self._settings,
            provider=self._provider,
            quotes=self._quotes,
            cycle_lock=self._cycle_lock,
            clock=self._clock,
            only_bot_id=only_bot_id,
            alerts=self._alerts,
        )

    async def _run(self) -> None:
        while True:
            try:
                result = await self.run_once()
                if result.outcomes:
                    logger.info(
                        "library_bot_cycle",
                        status=result.status.value,
                        bots=len(result.outcomes),
                        filled=sum(o.orders_filled for o in result.outcomes),
                        rejected=sum(o.orders_rejected for o in result.outcomes),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - see class docstring
                logger.error(
                    "library_bot_cycle_failed", error=str(exc), error_type=type(exc).__name__
                )
            await asyncio.sleep(self._interval_seconds)
