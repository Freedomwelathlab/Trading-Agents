from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
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
