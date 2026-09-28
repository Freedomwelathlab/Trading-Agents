"""Daily option chain snapshots: capture and read (Phase 100, D119).

**Why this exists.** Every options figure this platform has ever produced
for a past date was MODELLED: `pricing.py` prices a leg with Black-Scholes
from the underlying and a volatility someone assumed. There is no free
source of historical option chains to check that model against, and the
only way to have one later is to start writing the real chain down now.
This module does that - one snapshot per underlying per trading day, from
whatever `OptionChainProvider` the app is wired with (in practice Cboe's
delayed feed, because this Longbridge account cannot quote options,
`301604`).

**It is kept small on purpose.** The table grows forever on a metered
Postgres, so a capture keeps only:

* expiries within `max_dte` calendar days (default 60) - the research
  loop trades 7-45 DTE, and a 2028 LEAP adds rows no backtest here reads;
* strikes within `strike_band` of spot (default +-30%) - far wings carry
  no bid, move nothing, and are most of a chain's rows.

Measured on the real TQQQ document of 2026-09-25 (spot 79.85): 908 of
1,688 contracts pass, across 11 of 16 listed expiries - ~229k rows a year.

**One snapshot per underlying per TRADING DAY.** `trade_date` is the New
York date of the VENDOR's `as_of`, not of the read. A second capture for
the same trade date deletes the first inside the same transaction and
writes the new one, so "the after-close capture supersedes an intraday
one" holds without the table ever holding two versions of a day, and an
operator's on-demand capture can never make the daily one impossible.

**Nothing is invented.** A contract the vendor did not quote is stored
with NULLs, never zeros; the Greeks are the vendor's; a spot price that
cannot be found fails the capture (DATA_UNAVAILABLE) rather than drawing
the strike band around a guess. This module never commits: the caller
owns the transaction, exactly like `MarketDataStore`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.core.logging import get_logger
from apps.api.app.db.models import OptionChainSnapshot
from apps.api.app.marketdata.option_chain_provider import OptionChain, OptionChainProvider
from apps.api.app.marketdata.provider import DataUnavailableError
from apps.api.app.marketdata.store import MarketDataStore

logger = get_logger(__name__)

NEW_YORK = ZoneInfo("America/New_York")

DEFAULT_MAX_DTE = 60
DEFAULT_STRIKE_BAND = Decimal("0.30")

STORED_CLOSE_MAX_AGE_DAYS = 4
"""When the vendor prints no underlying price, the latest stored DAILY close
may stand in - but only if it is at most this many calendar days old
(covers a Friday close read on Monday plus one holiday). An older close
could sit tens of percent away on a 3x ETF, and a band drawn around it
would silently keep the wrong strikes."""

_COLUMNS_PER_ROW = 20
_MAX_ROWS_PER_INSERT = 32_767 // _COLUMNS_PER_ROW
"""Postgres caps one statement at 32,767 bind parameters; derived rather
than hard-coded so a new column shrinks the chunk automatically."""


class CaptureStatus(str, enum.Enum):  # noqa: UP042 (str mixin matches this codebase)
    CAPTURED = "captured"
    STALE_SOURCE = "stale_source"
    """The vendor's quotes are for a different trading day than the caller
    required (a holiday, or a feed that has not rolled). Nothing written:
    storing yesterday's quotes as today's snapshot would be fabrication by
    relabelling."""


@dataclass(frozen=True)
class SnapshotCaptureResult:
    underlying: str
    status: CaptureStatus
    trade_date: date
    as_of: datetime
    captured_at: datetime
    source: str
    underlying_price: Decimal
    spot_source: str
    rows_written: int
    rows_replaced: int
    expiries: tuple[date, ...]
    expiries_unavailable: tuple[date, ...]


def new_york_date(instant: datetime) -> date:
    if instant.tzinfo is None:
        raise ValueError("A naive datetime has no New York date; pass an aware instant.")
    return instant.astimezone(NEW_YORK).date()


async def _spot(
    session: AsyncSession, provider: Any, underlying: str, today: date
) -> tuple[Decimal, str]:
    getter = getattr(provider, "get_underlying_price", None)
    if getter is not None:
        price = await getter(underlying)
        if price is not None and price > 0:
            return Decimal(price), f"{provider.name}:underlying_price"
    latest = await MarketDataStore(session).get_latest_bars(underlying, bar_interval="1d", count=1)
    if latest:
        bar = latest[-1]
        age = (today - new_york_date(bar.ts)).days
        if 0 <= age <= STORED_CLOSE_MAX_AGE_DAYS and bar.close > 0:
            return bar.close, f"stored_1d_close:{new_york_date(bar.ts).isoformat()}"
    raise DataUnavailableError(
        f"No underlying price for {underlying}: the option vendor printed none and no stored "
        f"daily close is within {STORED_CLOSE_MAX_AGE_DAYS} days. The strike band cannot be "
        "drawn around a guess."
    )


def _rows(
    chain: OptionChain,
    *,
    spot: Decimal,
    band: Decimal,
    trade_date: date,
    captured_at: datetime,
) -> list[dict[str, Any]]:
    low, high = spot * (1 - band), spot * (1 + band)
    out: list[dict[str, Any]] = []
    for q in chain.quotes:
        if not low <= q.strike <= high:
            continue
        out.append(
            {
                "contract_symbol": q.contract_symbol,
                "as_of": chain.as_of,
                "underlying": chain.underlying,
                "trade_date": trade_date,
                "captured_at": captured_at,
                "source": chain.source[:32],
                "underlying_price": spot,
                "expiry": q.expiry,
                "right": q.right.value,
                "strike": q.strike,
                "bid": q.bid,
                "ask": q.ask,
                "last": q.last_price,
                "iv": q.implied_vol,
                "delta": q.delta,
                "gamma": q.gamma,
                "theta": q.theta,
                "vega": q.vega,
                "volume": q.volume,
                "open_interest": q.open_interest,
            }
        )
    return out


async def capture_chain_snapshot(
    session: AsyncSession,
    provider: OptionChainProvider,
    underlying: str,
    *,
    max_dte: int = DEFAULT_MAX_DTE,
    strike_band: Decimal = DEFAULT_STRIKE_BAND,
    now: datetime | None = None,
    require_trade_date: date | None = None,
) -> SnapshotCaptureResult:
    """Read the chain once and store today's filtered snapshot, replacing
    any earlier snapshot for the same underlying and trade date.

    Raises `DataUnavailableError` when there is nothing honest to store (no
    expiry in range, no spot, no contract in the band) and lets the
    provider's `VendorError` propagate - a vendor that failed mid-capture
    has not told us the chain is empty, and writing the half we got would
    look like a thin market rather than a failed read.

    `require_trade_date` is the scheduler's guard: when the vendor's quotes
    turn out to be for another day, nothing is written and the result says
    `stale_source`.

    Does not commit.
    """
    if max_dte < 0:
        raise ValueError("max_dte must be non-negative.")
    if not Decimal(0) < strike_band < Decimal(1):
        raise ValueError("strike_band must be a fraction strictly between 0 and 1.")
    captured_at = now or datetime.now(UTC)
    today = new_york_date(captured_at)
    horizon = today + timedelta(days=max_dte)

    listed = await provider.get_expiries(underlying)
    wanted = sorted(e for e in listed if today <= e <= horizon)
    if not wanted:
        raise DataUnavailableError(
            f"{provider.name} lists no {underlying} expiry within {max_dte} days of "
            f"{today.isoformat()}."
        )

    spot, spot_source = await _spot(session, provider, underlying, today)

    chains: list[OptionChain] = []
    unavailable: list[date] = []
    for expiry in wanted:
        try:
            chains.append(await provider.get_chain(underlying, expiry))
        except DataUnavailableError:
            # One expiry the vendor cannot show is a gap in this snapshot,
            # reported as such - not a reason to discard the other ten.
            unavailable.append(expiry)
    if not chains:
        raise DataUnavailableError(
            f"{provider.name} returned no chain for any {underlying} expiry in range."
        )

    as_of = max(c.as_of for c in chains)
    trade_date = new_york_date(as_of)
    source = chains[0].source

    if require_trade_date is not None and trade_date != require_trade_date:
        logger.info(
            "option_snapshot_stale_source",
            underlying=underlying,
            vendor_trade_date=trade_date.isoformat(),
            required=require_trade_date.isoformat(),
        )
        return SnapshotCaptureResult(
            underlying=underlying,
            status=CaptureStatus.STALE_SOURCE,
            trade_date=trade_date,
            as_of=as_of,
            captured_at=captured_at,
            source=source,
            underlying_price=spot,
            spot_source=spot_source,
            rows_written=0,
            rows_replaced=0,
            expiries=tuple(c.expiry for c in chains),
            expiries_unavailable=tuple(unavailable),
        )

    rows: list[dict[str, Any]] = []
    for chain in chains:
        rows.extend(
            _rows(
                chain,
                spot=spot,
                band=strike_band,
                trade_date=trade_date,
                captured_at=captured_at,
            )
        )
    if not rows:
        raise DataUnavailableError(
            f"No {underlying} contract lies within {strike_band:.0%} of spot {spot}."
        )

    replaced = await session.execute(
        delete(OptionChainSnapshot).where(
            OptionChainSnapshot.underlying == underlying,
            OptionChainSnapshot.trade_date == trade_date,
        )
    )
    for start in range(0, len(rows), _MAX_ROWS_PER_INSERT):
        stmt = pg_insert(OptionChainSnapshot).values(rows[start : start + _MAX_ROWS_PER_INSERT])
        # Only reachable if two underlyings ever shared an OCC symbol, which
        # OCC forbids; kept so a re-run can never fail on its own rows.
        stmt = stmt.on_conflict_do_nothing(index_elements=["contract_symbol", "as_of"])
        await session.execute(stmt)

    rows_replaced = int(getattr(replaced, "rowcount", 0) or 0)
    logger.info(
        "option_snapshot_captured",
        underlying=underlying,
        trade_date=trade_date.isoformat(),
        rows=len(rows),
        replaced=rows_replaced,
        source=source,
    )
    return SnapshotCaptureResult(
        underlying=underlying,
        status=CaptureStatus.CAPTURED,
        trade_date=trade_date,
        as_of=as_of,
        captured_at=captured_at,
        source=source,
        underlying_price=spot,
        spot_source=spot_source,
        rows_written=len(rows),
        rows_replaced=rows_replaced,
        expiries=tuple(c.expiry for c in chains),
        expiries_unavailable=tuple(unavailable),
    )


async def list_snapshot_dates(session: AsyncSession, underlying: str) -> list[date]:
    """Trade dates with a stored snapshot, oldest first."""
    rows = await session.execute(
        select(OptionChainSnapshot.trade_date)
        .where(OptionChainSnapshot.underlying == underlying)
        .group_by(OptionChainSnapshot.trade_date)
        .order_by(OptionChainSnapshot.trade_date.asc())
    )
    return list(rows.scalars().all())


async def load_snapshot(
    session: AsyncSession, underlying: str, trade_date: date | None = None
) -> list[OptionChainSnapshot]:
    """One trade date's rows (the latest stored date when none is named),
    ordered by expiry, strike, right. Empty means no snapshot - never a
    neighbouring day's."""
    if trade_date is None:
        trade_date = await session.scalar(
            select(func.max(OptionChainSnapshot.trade_date)).where(
                OptionChainSnapshot.underlying == underlying
            )
        )
        if trade_date is None:
            return []
    rows = await session.execute(
        select(OptionChainSnapshot)
        .where(
            OptionChainSnapshot.underlying == underlying,
            OptionChainSnapshot.trade_date == trade_date,
        )
        .order_by(
            OptionChainSnapshot.expiry.asc(),
            OptionChainSnapshot.strike.asc(),
            OptionChainSnapshot.right.asc(),
        )
    )
    return list(rows.scalars().all())


async def load_snapshots_between(
    session: AsyncSession, underlying: str, start: date, end: date
) -> list[OptionChainSnapshot]:
    """Every stored row for trade dates in [start, end] - the backtest's read."""
    rows = await session.execute(
        select(OptionChainSnapshot).where(
            OptionChainSnapshot.underlying == underlying,
            OptionChainSnapshot.trade_date >= start,
            OptionChainSnapshot.trade_date <= end,
        )
    )
    return list(rows.scalars().all())


async def latest_captured_at(
    session: AsyncSession, underlying: str, trade_date: date
) -> datetime | None:
    """When the stored snapshot for this trade date was read, or None."""
    return await session.scalar(
        select(func.max(OptionChainSnapshot.captured_at)).where(
            OptionChainSnapshot.underlying == underlying,
            OptionChainSnapshot.trade_date == trade_date,
        )
    )
