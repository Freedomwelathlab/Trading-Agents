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
from typing import Literal, Protocol

from pydantic import BaseModel, Field, field_validator

BarInterval = Literal["1m", "5m", "15m", "30m", "1h", "1d"]
"""The bar intervals this system recognizes (Phase 70, D088).

**One definition, imported everywhere.** Before this phase each request
schema carried its own `Literal["1d"]`, which was correct while `1d` was
genuinely the only thing that existed but meant six places had to be
edited in lockstep to add an interval - and a missed one would be a 422 on
an interval the rest of the system supported perfectly.

The vocabulary is CLOSED for the same reason the indicator vocabulary is:
`market_data_bars.bar_interval` is a plain string column (migration 0016,
deliberately not a Postgres enum, so adding an interval needs no
`ALTER TYPE`), which by itself would accept `"1 day"`, `"daily"` and `"1D"`
as three distinct intervals that never match each other on read. These six
strings are the spellings, and `MarketDataStore` matches them exactly and
never coerces.

**Listed here does not mean ingested.** An interval is available for a
given symbol only if bars have actually been backfilled for that exact
`(symbol, bar_interval)` pair. Asking for a window with no bars is not a
schema error and is no longer refused as one - it produces a real,
persisted FAILED run whose `error_detail` names the missing range, which
says considerably more than a 422 on the interval itself would.

**Vendor support is per-provider and is asserted nowhere here.** This type
says what this system can store and evaluate. Whether a particular vendor
returns 5m candles for a particular symbol is that adapter's business, and
`LongbridgeBarBackfillProvider` refuses an interval it cannot map rather
than quietly substituting a daily one.
"""


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
