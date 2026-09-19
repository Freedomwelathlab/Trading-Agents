"""The Autotrade Bot runner (Phase 81, D098): an in-process periodic task,
owned by the FastAPI lifespan, that gives every ACTIVE bot one
`engine.run_bot_cycle` per interval.

A sibling of `StrategyDeploymentRunner` with the same safety shape:

- **Only ACTIVE bots.** Reaching ACTIVE requires the explicit approval
  action in `autotrade/service.py`.
- **One transaction per bot, ALWAYS a terminal run row** (the D072
  posture): the run row is created and flushed first, the engine resolves
  it, a broad outer `except` resolves it to FAILED, never a stranded row.
- **Cross-worker exclusion** through the same Postgres advisory-lock class
  the other three background jobs use, on this job's own objid.
- **No vendor, no rows.** When no equity market-data vendor is wired the
  loop logs once per cycle and writes nothing — a bot that cannot see a
  price has nothing to record every sixty seconds forever.
- **Paper brokers only.** A bot on a live broker gets a
  `SKIPPED_LIVE_NOT_SUPPORTED` row before any market data or broker is
  touched. D087's live path is a separate, separately-gated build.

DEFAULT: ENABLED, because a runner with no approved bots does nothing,
and every bot needs a human approval before it can act — the gate is on
the bot, not on the loop. `AUTOTRADE_RUNNER_ENABLED=false` turns the loop
off entirely.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.app.autotrade.engine import BotCycleOutcome, run_bot_cycle
from apps.api.app.core.config import Settings
from apps.api.app.core.logging import get_logger
from apps.api.app.db.models import (
    AutotradeBot,
    AutotradeBotRun,
    AutotradeBotRunStatus,
    AutotradeBotStatus,
)
from apps.api.app.marketdata.bar_router import BarBackfillRouter
from apps.api.app.marketdata.news_provider import NewsProvider
from apps.api.app.portfolio.cycle_lock import SnapshotCycleLock, SnapshotCycleLockDecision

logger = get_logger(__name__)

AUTOTRADE_RUNNER_LOCK_OBJID = 1635017844
"""ASCII ``b"abot"`` big-endian (0x61626f74) — this job's own advisory-lock
object key, same classid namespace as the other background jobs."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def build_autotrade_runner_cycle_lock(*, enabled: bool = True) -> SnapshotCycleLock:
    return SnapshotCycleLock(
        enabled=enabled, objid=AUTOTRADE_RUNNER_LOCK_OBJID, job_name="autotrade_bot_runner"
    )


class AutotradeCycleStatus(str, Enum):  # noqa: UP042
    RAN = "ran"
    SKIPPED_LOCK_HELD = "skipped_lock_held"
    SKIPPED_NOT_CONFIGURED = "skipped_not_configured"


@dataclass
class AutotradeCycleResult:
    status: AutotradeCycleStatus
    outcomes: tuple[BotCycleOutcome, ...] = field(default_factory=tuple)
    detail: str | None = None

    @property
    def trades_opened(self) -> int:
        return sum(o.trades_opened for o in self.outcomes)

    @property
    def trades_closed(self) -> int:
        return sum(o.trades_closed for o in self.outcomes)


async def run_bot_isolated(
    session_factory: async_sessionmaker[AsyncSession],
    bot_id: uuid.UUID,
    *,
    settings: Settings,
    clock: Callable[[], datetime],
    bar_router: BarBackfillRouter | None,
    news_provider: NewsProvider | None,
) -> BotCycleOutcome | None:
    """One bot, one transaction, always a terminal run row."""
    started = clock()
    async with session_factory() as session:
        bot = await session.get(AutotradeBot, bot_id)
        if bot is None:
            return None
        run = AutotradeBotRun(
            id=uuid.uuid4(),
            bot_id=bot.id,
            status=AutotradeBotRunStatus.FAILED,  # provisional; resolved below
            started_at=started,
        )
        session.add(run)
        await session.flush()
        try:
            outcome = await run_bot_cycle(
                session, bot, run,
                settings=settings, clock=clock,
                bar_router=bar_router, news_provider=news_provider,
            )
            await session.commit()
            return outcome
        except Exception as exc:  # noqa: BLE001 - the run row must resolve
            await session.rollback()
            async with session_factory() as fresh:
                fresh.add(
                    AutotradeBotRun(
                        id=run.id,
                        bot_id=bot_id,
                        status=AutotradeBotRunStatus.FAILED,
                        started_at=started,
                        completed_at=clock(),
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
                await fresh.commit()
            logger.error(
                "autotrade_bot_cycle_failed",
                bot_id=str(bot_id), error=str(exc), error_type=type(exc).__name__,
            )
            return BotCycleOutcome(
                bot_id=bot_id, run_id=run.id, status=AutotradeBotRunStatus.FAILED,
                detail=f"{type(exc).__name__}: {exc}",
            )


async def run_autotrade_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    bar_router: BarBackfillRouter | None,
    news_provider: NewsProvider | None = None,
    cycle_lock: SnapshotCycleLock | None = None,
    clock: Callable[[], datetime] = utc_now,
    only_bot_id: uuid.UUID | None = None,
) -> AutotradeCycleResult:
    """One pass over every ACTIVE bot (or one bot, for the manual
    "scan now" route). Each bot is its own transaction."""
    lock = cycle_lock if cycle_lock is not None else SnapshotCycleLock(enabled=False)

    if bar_router is None or "longbridge" not in bar_router.configured_vendors:
        if only_bot_id is None:
            # The scheduled loop writes nothing; the manual route still
            # produces a run row (below) so the operator sees WHY.
            return AutotradeCycleResult(
                status=AutotradeCycleStatus.SKIPPED_NOT_CONFIGURED,
                detail="NOT_CONFIGURED: no equity market-data vendor is wired (LONGPORT_* unset)",
            )

    async with session_factory() as lock_session, lock.hold(lock_session) as decision:
        if decision is SnapshotCycleLockDecision.SKIPPED_LOCK_HELD:
            return AutotradeCycleResult(status=AutotradeCycleStatus.SKIPPED_LOCK_HELD)

        async with session_factory() as session:
            query = select(AutotradeBot.id)
            if only_bot_id is not None:
                query = query.where(AutotradeBot.id == only_bot_id)
            else:
                query = query.where(AutotradeBot.status == AutotradeBotStatus.ACTIVE)
            bot_ids = list((await session.execute(query)).scalars().all())

        outcomes: list[BotCycleOutcome] = []
        for bot_id in bot_ids:
            outcome = await run_bot_isolated(
                session_factory, bot_id, settings=settings, clock=clock,
                bar_router=bar_router, news_provider=news_provider,
            )
            if outcome is not None:
                outcomes.append(outcome)
        return AutotradeCycleResult(status=AutotradeCycleStatus.RAN, outcomes=tuple(outcomes))


class AutotradeBotRunner:
    """Periodic loop; runs one cycle immediately on `start()` then every
    `interval_seconds`. Never dies on a transient error."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        interval_seconds: int,
        bar_router: BarBackfillRouter | None,
        news_provider: NewsProvider | None = None,
        cycle_lock: SnapshotCycleLock | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive.")
        self._session_factory = session_factory
        self._settings = settings
        self._interval_seconds = interval_seconds
        self._bar_router = bar_router
        self._news_provider = news_provider
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
            raise RuntimeError("AutotradeBotRunner.start() called twice")
        self._task = asyncio.create_task(self._run(), name="autotrade-bot-runner")
        logger.info("autotrade_bot_runner_started", interval_seconds=self._interval_seconds)

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
        logger.info("autotrade_bot_runner_stopped")

    async def run_once(self, *, only_bot_id: uuid.UUID | None = None) -> AutotradeCycleResult:
        return await run_autotrade_cycle(
            self._session_factory,
            settings=self._settings,
            bar_router=self._bar_router,
            news_provider=self._news_provider,
            cycle_lock=self._cycle_lock,
            clock=self._clock,
            only_bot_id=only_bot_id,
        )

    async def _run(self) -> None:
        while True:
            try:
                result = await self.run_once()
                logger.info(
                    "autotrade_bot_cycle",
                    status=result.status.value,
                    bots=len(result.outcomes),
                    opened=result.trades_opened,
                    closed=result.trades_closed,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - see class docstring
                logger.error(
                    "autotrade_bot_cycle_failed", error=str(exc), error_type=type(exc).__name__
                )
            await asyncio.sleep(self._interval_seconds)
