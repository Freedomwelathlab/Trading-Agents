"""The options bot runner (Phase 102, D122): an in-process periodic task,
owned by the FastAPI lifespan, that gives every ACTIVE options bot one
`engine.run_options_bot_cycle` per interval.

Same safety shape as `autotrade/runner.py`:

- **Only ACTIVE bots**, and ACTIVE requires the explicit approval action.
- **One transaction per bot, ALWAYS a terminal run row**: created and
  flushed first, resolved by the engine, and a broad `except` resolves it
  to FAILED in a fresh session rather than leaving it stranded.
- **Cross-worker exclusion** through the shared advisory-lock class, on
  this job's own objid (``b"obot"``).
- **Closed-market rows throttled** to one an hour per bot.

DEFAULT: DISABLED (`OPTIONS_BOT_RUNNER_ENABLED=false`). Unlike the
Autotrade runner this loop is off until the operator switches it on - it
is new, it trades from a ~15-minute-delayed feed, and it settles expiries.
With it off, a bot only ever runs when a person presses "run now".
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app.core.config import Settings
from apps.api.app.core.logging import get_logger
from apps.api.app.db.models import (
    OptionsBot,
    OptionsBotRun,
    OptionsBotRunStatus,
    OptionsBotStatus,
)
from apps.api.app.marketdata.option_chain_provider import OptionChainProvider
from apps.api.app.options_bot.engine import (
    OptionsBotCycleOutcome,
    market_open,
    run_options_bot_cycle,
)
from apps.api.app.portfolio.cycle_lock import SnapshotCycleLock, SnapshotCycleLockDecision

logger = get_logger(__name__)

CLOSED_MARKET_ROW_INTERVAL = timedelta(hours=1)

OPTIONS_BOT_RUNNER_LOCK_OBJID = 1868722036
"""ASCII ``b"obot"`` big-endian (0x6f626f74) - this job's own advisory-lock
object key, same classid namespace as the other background jobs."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def build_options_bot_runner_cycle_lock(*, enabled: bool = True) -> SnapshotCycleLock:
    return SnapshotCycleLock(
        enabled=enabled, objid=OPTIONS_BOT_RUNNER_LOCK_OBJID, job_name="options_bot_runner"
    )


class OptionsBotCycleStatus(str, Enum):  # noqa: UP042
    RAN = "ran"
    SKIPPED_LOCK_HELD = "skipped_lock_held"


@dataclass
class OptionsBotCycleResult:
    status: OptionsBotCycleStatus
    outcomes: tuple[OptionsBotCycleOutcome, ...] = field(default_factory=tuple)
    detail: str | None = None


async def _recent_closed_row(session: AsyncSession, bot_id: uuid.UUID, now: datetime) -> bool:
    latest = (
        await session.execute(
            select(OptionsBotRun)
            .where(OptionsBotRun.bot_id == bot_id)
            .order_by(OptionsBotRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return (
        latest is not None
        and latest.status is OptionsBotRunStatus.SKIPPED_MARKET_CLOSED
        and now - latest.started_at < CLOSED_MARKET_ROW_INTERVAL
    )


async def run_options_bot_isolated(
    session_factory: async_sessionmaker[AsyncSession],
    bot_id: uuid.UUID,
    *,
    settings: Settings,
    clock: Callable[[], datetime],
    provider: OptionChainProvider | None,
    bar_router: Any | None,
    throttle_closed: bool = True,
) -> OptionsBotCycleOutcome | None:
    started = clock()
    async with session_factory() as session:
        bot = await session.get(OptionsBot, bot_id)
        if bot is None:
            return None
        if (
            throttle_closed
            and bot.status is OptionsBotStatus.ACTIVE
            and not market_open(started)
            and await _recent_closed_row(session, bot.id, started)
        ):
            return None
        run = OptionsBotRun(
            id=uuid.uuid4(),
            bot_id=bot.id,
            status=OptionsBotRunStatus.FAILED,  # provisional; resolved below
            started_at=started,
        )
        session.add(run)
        await session.flush()
        try:
            outcome = await run_options_bot_cycle(
                session, bot, run, settings=settings, clock=clock, provider=provider,
                bar_router=bar_router,
            )
            await session.commit()
            return outcome
        except Exception as exc:  # noqa: BLE001 - the run row must resolve
            await session.rollback()
            async with session_factory() as fresh:
                fresh.add(
                    OptionsBotRun(
                        id=run.id,
                        bot_id=bot_id,
                        status=OptionsBotRunStatus.FAILED,
                        started_at=started,
                        completed_at=clock(),
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
                await fresh.commit()
            logger.error(
                "options_bot_cycle_failed",
                bot_id=str(bot_id), error=str(exc), error_type=type(exc).__name__,
            )
            return OptionsBotCycleOutcome(
                bot_id=bot_id, run_id=run.id, status=OptionsBotRunStatus.FAILED,
                detail=f"{type(exc).__name__}: {exc}",
            )


async def run_options_bot_cycle_all(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    provider: OptionChainProvider | None,
    bar_router: Any | None = None,
    cycle_lock: SnapshotCycleLock | None = None,
    clock: Callable[[], datetime] = utc_now,
    only_bot_id: uuid.UUID | None = None,
) -> OptionsBotCycleResult:
    """One pass over every ACTIVE bot (or one bot, for "run now")."""
    lock = cycle_lock if cycle_lock is not None else SnapshotCycleLock(enabled=False)
    async with session_factory() as lock_session, lock.hold(lock_session) as decision:
        if decision is SnapshotCycleLockDecision.SKIPPED_LOCK_HELD:
            return OptionsBotCycleResult(status=OptionsBotCycleStatus.SKIPPED_LOCK_HELD)
        async with session_factory() as session:
            query = select(OptionsBot.id)
            if only_bot_id is not None:
                query = query.where(OptionsBot.id == only_bot_id)
            else:
                query = query.where(OptionsBot.status == OptionsBotStatus.ACTIVE)
            bot_ids = list((await session.execute(query)).scalars().all())
        outcomes: list[OptionsBotCycleOutcome] = []
        for bot_id in bot_ids:
            outcome = await run_options_bot_isolated(
                session_factory, bot_id, settings=settings, clock=clock, provider=provider,
                bar_router=bar_router, throttle_closed=only_bot_id is None,
            )
            if outcome is not None:
                outcomes.append(outcome)
        return OptionsBotCycleResult(status=OptionsBotCycleStatus.RAN, outcomes=tuple(outcomes))


class OptionsBotRunner:
    """Periodic loop; one cycle immediately on `start()` then every
    `interval_seconds`. Never dies on a transient error."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        interval_seconds: int,
        provider: OptionChainProvider | None,
        bar_router: Any | None = None,
        cycle_lock: SnapshotCycleLock | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive.")
        self._session_factory = session_factory
        self._settings = settings
        self._interval_seconds = interval_seconds
        self._provider = provider
        self._bar_router = bar_router
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
            raise RuntimeError("OptionsBotRunner.start() called twice")
        self._task = asyncio.create_task(self._run(), name="options-bot-runner")
        logger.info("options_bot_runner_started", interval_seconds=self._interval_seconds)

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
        logger.info("options_bot_runner_stopped")

    async def run_once(self, *, only_bot_id: uuid.UUID | None = None) -> OptionsBotCycleResult:
        return await run_options_bot_cycle_all(
            self._session_factory,
            settings=self._settings,
            provider=self._provider,
            bar_router=self._bar_router,
            cycle_lock=self._cycle_lock,
            clock=self._clock,
            only_bot_id=only_bot_id,
        )

    async def _run(self) -> None:
        while True:
            try:
                result = await self.run_once()
                logger.info(
                    "options_bot_cycle",
                    status=result.status.value,
                    bots=len(result.outcomes),
                    opened=sum(o.trades_opened for o in result.outcomes),
                    closed=sum(o.trades_closed for o in result.outcomes),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - see class docstring
                logger.error(
                    "options_bot_cycle_failed", error=str(exc), error_type=type(exc).__name__
                )
            await asyncio.sleep(self._interval_seconds)
