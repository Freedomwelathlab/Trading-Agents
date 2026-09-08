"""SQLAlchemy-backed reader/writer for market_data_bars (Phase 53,
docs/DECISIONS.md D070). MarketDataStore implements the read side of
apps.api.app.marketdata.bar_provider.HistoricalBarProvider.

Unlike the vendor Provider classes elsewhere in this package
(MarketDataProvider/HistoryProvider), which are built once at application
startup around a long-lived vendor client, a Postgres AsyncSession is
request-scoped, not process-scoped - so this class is constructed fresh
per request/call around whichever session the caller already holds
(apps/api/app/db/base.py::get_session), the same way
apps/api/app/execution/persistence.py's functions take a session
explicitly rather than owning one.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import MarketDataBar
from apps.api.app.marketdata.bar_provider import Bar


def _start_of_day_utc(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def _end_of_day_utc(day: date) -> datetime:
    return datetime.combine(day, time.max, tzinfo=UTC)


def _row_to_bar(row: MarketDataBar) -> Bar:
    return Bar(
        symbol=row.symbol,
        bar_interval=row.bar_interval,
        ts=row.ts,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
        source=row.source,
    )


class MarketDataStore:
    """Implements HistoricalBarProvider against `market_data_bars`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_bars(
        self, symbol: str, *, bar_interval: str, start_date: date, end_date: date
    ) -> list[Bar]:
        """Real, persisted bars only - oldest first. An empty result means
        no bars have been ingested for this exact (symbol, bar_interval)
        over this window; callers must treat that as a gap to backfill or
        an INSUFFICIENT_DATA result, never pad or interpolate."""
        rows = (
            (
                await self._session.execute(
                    select(MarketDataBar)
                    .where(
                        MarketDataBar.symbol == symbol,
                        MarketDataBar.bar_interval == bar_interval,
                        MarketDataBar.ts >= _start_of_day_utc(start_date),
                        MarketDataBar.ts <= _end_of_day_utc(end_date),
                    )
                    .order_by(MarketDataBar.ts.asc())
                )
            )
            .scalars()
            .all()
        )
        return [_row_to_bar(row) for row in rows]

    async def upsert_bars(self, bars: Sequence[Bar]) -> int:
        """Idempotent: re-ingesting an overlapping window overwrites each
        (symbol, bar_interval, ts) row with the latest vendor figures
        rather than duplicating it or erroring (D070). Returns the number
        of bars submitted for write - Postgres does not distinguish
        insert-vs-update counts through a single ON CONFLICT DO UPDATE
        statement in a way this method reports separately."""
        if not bars:
            return 0

        values = [
            {
                "symbol": bar.symbol,
                "bar_interval": bar.bar_interval,
                "ts": bar.ts,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "source": bar.source,
            }
            for bar in bars
        ]
        stmt = pg_insert(MarketDataBar).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[MarketDataBar.symbol, MarketDataBar.bar_interval, MarketDataBar.ts],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "source": stmt.excluded.source,
            },
        )
        await self._session.execute(stmt)
        return len(values)
