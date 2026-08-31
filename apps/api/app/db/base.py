from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from apps.api.app.core.config import get_settings


class Base(DeclarativeBase):
    pass


_engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _session_factory() as session:
        yield session


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """The sessionmaker itself, for background work that has no request to
    hang a `Depends(get_session)` off (Phase 27's portfolio snapshot
    scheduler - apps/api/app/portfolio/scheduler.py). Returns the same
    factory `get_session` uses, so background and request-scoped sessions
    share one engine and one connection pool rather than the scheduler
    quietly opening a second pool of its own."""
    return _session_factory


def get_engine() -> AsyncEngine:
    """The one process-wide engine, for callers that need a connection
    without a session on top of it.

    Phase 41 (D054)'s readiness probe is the only such caller today: it
    wants to know whether *this app's own connection pool* can reach
    Postgres, so it must use this exact engine rather than dialling the
    database independently - a probe that opened its own connection could
    report "ready" while the pool every real request draws from is
    exhausted or broken, which is precisely the false-positive the probe
    exists to prevent."""
    return _engine
