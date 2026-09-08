"""Orchestrates one on-demand historical-bar backfill job (Phase 53,
docs/DECISIONS.md D070): creates a MarketDataBackfillJob row, fetches real
OHLCV bars from a BarBackfillProvider (the vendor boundary - Longbridge is
the only implementation, see
apps/api/app/marketdata/providers/longbridge.py's LongbridgeBarBackfillProvider),
upserts them into market_data_bars via MarketDataStore, and records the
outcome on the same job row.

Never fabricates a bar: a vendor failure or an empty vendor response
(DataUnavailableError/VendorError - apps/api/app/marketdata/provider.py's
shared error types) is recorded as FAILED with a real error_detail, never
silently treated as "zero bars ingested, success" (docs/TRADING_SAFETY.md's
no-fabrication rule).

Runs synchronously, to completion, within the HTTP request that calls it -
there is no scheduling/automation here, or anywhere in this codebase
outside the snapshot scheduler's own documented single-worker limitation.
This is a manual, caller-triggered job, exactly once per call, the same
posture D025's backtest engine takes toward its own execution. Automatic
recurring backfill is explicit future work (Phase 54+), not built here.
"""

import uuid
from datetime import UTC, date
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import BackfillJobStatus, MarketDataBackfillJob
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.store import MarketDataStore


class BarBackfillProvider(Protocol):
    """Vendor-boundary port for a backfill job's data source - deliberately
    separate from HistoricalBarProvider (bar_provider.py), which reads back
    OUT of the store this job writes INTO. LongbridgeBarBackfillProvider
    (providers/longbridge.py) is the only implementation."""

    name: str

    async def get_bars(
        self, symbol: str, *, bar_interval: str, start_date: date, end_date: date
    ) -> list[Bar]: ...


async def run_backfill_job(
    session: AsyncSession,
    *,
    symbol: str,
    bar_interval: str,
    start_date: date,
    end_date: date,
    provider: BarBackfillProvider,
    requested_by_user_id: uuid.UUID | None,
) -> MarketDataBackfillJob:
    """Returns the job row, already committed and in a terminal status
    (SUCCEEDED or FAILED) - there is no PENDING/RUNNING state visible to a
    caller of this function, since nothing async is handed off; those
    statuses exist on the row for the brief window before the vendor call
    returns, and for a future job type that genuinely runs in the
    background."""
    job = MarketDataBackfillJob(
        symbol=symbol,
        bar_interval=bar_interval,
        requested_start_date=start_date,
        requested_end_date=end_date,
        status=BackfillJobStatus.RUNNING,
        requested_by_user_id=requested_by_user_id,
    )
    session.add(job)
    await session.flush()

    try:
        bars = await provider.get_bars(
            symbol, bar_interval=bar_interval, start_date=start_date, end_date=end_date
        )
    except (VendorError, DataUnavailableError) as exc:
        job.status = BackfillJobStatus.FAILED
        job.error_detail = str(exc)
        await session.commit()
        return job

    store = MarketDataStore(session)
    ingested = await store.upsert_bars(bars)

    job.bars_ingested = ingested
    job.earliest_bar_date = min(bar.ts.astimezone(UTC).date() for bar in bars)
    job.latest_bar_date = max(bar.ts.astimezone(UTC).date() for bar in bars)
    job.status = BackfillJobStatus.SUCCEEDED
    await session.commit()
    return job
