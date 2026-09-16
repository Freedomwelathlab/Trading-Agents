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

_COLUMNS_PER_BAR = 9
"""Columns bound per row by `upsert_bars` - symbol, bar_interval, ts, open,
high, low, close, volume, source. Kept beside the limit below so the two
cannot drift if a column is ever added."""

_POSTGRES_MAX_BIND_PARAMS = 32_767
"""Hard ceiling in the Postgres wire protocol on bind parameters in one
statement. Not a tunable - exceeding it is a driver-level error, not slow."""

_MAX_BARS_PER_INSERT = _POSTGRES_MAX_BIND_PARAMS // _COLUMNS_PER_BAR
"""3,640 bars per statement. Derived rather than hardcoded so that adding a
column to `market_data_bars` shrinks the chunk automatically instead of
silently reintroducing the overflow."""


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

    async def get_latest_bars(self, symbol: str, *, bar_interval: str, count: int) -> list[Bar]:
        """The most recent `count` bars for (symbol, bar_interval), returned
        OLDEST-FIRST (so it drops straight into the same evaluators `get_bars`
        feeds). Fewer than `count` if that much history has not been ingested -
        callers handle a short series explicitly, exactly as `get_bars`'s own
        contract requires; never pad.

        Deliberately NOT expressible as a `get_bars` call: "the newest N bars"
        has no date range a caller could name without already knowing which
        calendar days have bars, which is the very thing the store is being
        asked. The window is therefore selected NEWEST-first in SQL - so
        Postgres reads only `count` rows off the (symbol, bar_interval, ts)
        index rather than the symbol's whole history - and reversed here, in
        Python, over that already-bounded list.

        `bar_interval` is matched exactly, never coerced: "the latest 1d bars"
        and "the latest 1m bars" are different series, and silently answering
        with the wrong one would feed a strategy bars it was never evaluated
        against.
        """
        if count <= 0:
            return []
        rows = (
            (
                await self._session.execute(
                    select(MarketDataBar)
                    .where(
                        MarketDataBar.symbol == symbol,
                        MarketDataBar.bar_interval == bar_interval,
                    )
                    .order_by(MarketDataBar.ts.desc())
                    .limit(count)
                )
            )
            .scalars()
            .all()
        )
        return [_row_to_bar(row) for row in reversed(rows)]

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

        # CHUNKED (Phase 71, D089). A single multi-row INSERT binds
        # `_COLUMNS_PER_BAR` parameters per bar, and Postgres' wire protocol
        # caps one statement at 32767 of them - so a write of more than
        # ~3640 bars fails outright with "the number of query arguments
        # cannot exceed 32767".
        #
        # Phase 53 never hit this because only DAILY bars were ever
        # ingested: a decade of them is under 3700 rows. Intraday makes it
        # ordinary - 185 days of hourly BTC is 4,440 bars, and 180 days of
        # 5-minute bars is over 50,000 - so the ceiling is now reached by a
        # completely routine backfill rather than an exotic one.
        #
        # Every chunk goes through the SAME session and therefore the same
        # transaction, so this stays atomic: a failure partway through rolls
        # the whole backfill back rather than leaving a half-ingested window
        # that later looks like a real data gap.
        for start in range(0, len(values), _MAX_BARS_PER_INSERT):
            chunk = values[start : start + _MAX_BARS_PER_INSERT]
            stmt = pg_insert(MarketDataBar).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    MarketDataBar.symbol,
                    MarketDataBar.bar_interval,
                    MarketDataBar.ts,
                ],
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
