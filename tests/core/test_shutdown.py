"""Ordered graceful shutdown (Phase 42, docs/DECISIONS.md D056).

A real full-process shutdown (SIGTERM to uvicorn, grace period, exit) is
not something this harness can assert on from inside the process being
shut down, so the ordering guarantee is pinned here at the seam that
actually encodes it: the lifespan's teardown block. The spies record a
single shared call log, so the assertion is on the real *relative order* of
the two real calls the block makes — not on each one merely having
happened.

The one thing that must never regress is the direction: the scheduler owns
background work that borrows sessions from the engine's pool, so it has to
be fully stopped before that pool is disposed.
"""

import asyncio
import inspect

from apps.api.app.main import app, lifespan
from apps.api.app.portfolio.scheduler import PortfolioSnapshotScheduler


class _SpyScheduler:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def stop(self) -> None:
        # A real `stop()` awaits its task; yielding here means a wrongly
        # ordered implementation that disposed without awaiting would be
        # visible in `calls` rather than hidden by the coroutine finishing
        # synchronously.
        await asyncio.sleep(0)
        self._calls.append("scheduler_stopped")


class _SpyEngine:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def dispose(self) -> None:
        self._calls.append("engine_disposed")


async def test_the_scheduler_is_stopped_before_the_engine_is_disposed(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("apps.api.app.main.get_engine", lambda: _SpyEngine(calls))

    async with lifespan(app):
        # Installed after startup so the real startup path (which leaves
        # this None unless the scheduler is enabled) is unchanged; the
        # teardown block reads `app.state` exactly as it does in production.
        app.state.portfolio_snapshot_scheduler = _SpyScheduler(calls)

    assert calls == ["scheduler_stopped", "engine_disposed"]


async def test_the_engine_is_disposed_even_with_no_scheduler_running(monkeypatch):
    """The scheduler is disabled by default, so this is the common path."""
    calls: list[str] = []
    monkeypatch.setattr("apps.api.app.main.get_engine", lambda: _SpyEngine(calls))

    async with lifespan(app):
        app.state.portfolio_snapshot_scheduler = None

    assert calls == ["engine_disposed"]


async def test_scheduler_stop_awaits_its_task_rather_than_only_cancelling():
    """The ordering above is only meaningful if `stop()` really waits.

    If it merely signalled cancellation and returned, the engine could be
    disposed while a cycle was still mid-flight holding a pooled session.
    This drives the real `PortfolioSnapshotScheduler.stop()` against a task
    that needs a turn of the loop to unwind, and asserts the task is
    genuinely finished by the time `stop()` returns.
    """
    scheduler = PortfolioSnapshotScheduler.__new__(PortfolioSnapshotScheduler)
    finished = asyncio.Event()

    async def _slow_loop() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Simulates a cycle that still has unwinding to do after the
            # cancellation is delivered.
            await asyncio.sleep(0)
            finished.set()
            raise

    scheduler._task = asyncio.create_task(_slow_loop())
    await asyncio.sleep(0)

    await scheduler.stop()

    assert finished.is_set()
    assert scheduler._task is None


async def test_stopping_a_scheduler_that_never_started_is_a_no_op():
    scheduler = PortfolioSnapshotScheduler.__new__(PortfolioSnapshotScheduler)
    scheduler._task = None
    await scheduler.stop()
    assert scheduler._task is None


def test_the_lifespan_teardown_is_reachable_only_after_startup():
    """Guard against the teardown block being moved above `yield`."""
    source = inspect.getsource(lifespan)
    yield_at = source.index("    yield")
    assert source.index("portfolio_snapshot_scheduler.stop()") > yield_at
    assert source.index("get_engine().dispose()") > yield_at
    assert source.index("portfolio_snapshot_scheduler.stop()") < source.index(
        "get_engine().dispose()"
    ), "the scheduler must be stopped before the engine it uses is disposed"


def test_the_scheduler_exposes_the_awaitable_stop_the_lifespan_relies_on():
    assert inspect.iscoroutinefunction(PortfolioSnapshotScheduler.stop)
