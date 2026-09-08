"""Historical OHLCV bar store port (Phase 53, docs/DECISIONS.md D070) -
closes the gap HistoryProvider (history_provider.py, D021) leaves: that
Protocol only exposes "the most recent N daily closes as of now," closes
only, no OHLV, no arbitrary date range. This Protocol is the read contract
for a PERSISTED bar store instead - apps/api/app/marketdata/store.py's
MarketDataStore is the only implementation today.

Deliberately separate from, not a replacement for, HistoryProvider: the
live analyst layer (D021) still reads through HistoryProvider for its one
most-recent-N-closes use case, unaffected by anything in this module. A
HistoricalBarProvider answers "what real bars exist for this symbol over
this date range," which HistoryProvider structurally cannot (it has no
date-range parameter and no notion of a window that doesn't end "now").
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, Field, field_validator


class Bar(BaseModel):
    """One real OHLCV bar, exactly as ingested from a vendor - never
    interpolated, padded, or estimated (docs/TRADING_SAFETY.md's
    no-fabrication rule). `open`/`high`/`low`/`volume` are nullable
    because a vendor may in principle return a close-only record; `close`
    is the one field every bar must have to be meaningful at all."""

    symbol: str = Field(min_length=1)
    bar_interval: str = Field(min_length=1)
    ts: datetime
    """The bar's session timestamp, timezone-aware. For a daily bar this
    is the vendor's own timestamp for that trading day - never a
    fabricated close time."""
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal = Field(gt=0)
    volume: int | None = None
    source: str = Field(min_length=1)
    """Which provider produced this bar (e.g. "longbridge") - kept on the
    bar itself so a suspicious or wrong value can always be traced back to
    where it came from, matching MarketSnapshot.source's identical
    reasoning."""

    @field_validator("ts")
    @classmethod
    def _must_be_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("ts must be timezone-aware")
        return v


class HistoricalBarProvider(Protocol):
    async def get_bars(
        self, symbol: str, *, bar_interval: str, start_date: date, end_date: date
    ) -> list[Bar]:
        """Real, persisted bars in [start_date, end_date], oldest first.
        Returns fewer than the full requested window - including an empty
        list - when that much history has not been ingested. Callers must
        treat a short or empty result as a real data gap (INSUFFICIENT_DATA
        for whatever they were trying to compute), never pad, interpolate,
        or guess a missing bar."""
        ...
