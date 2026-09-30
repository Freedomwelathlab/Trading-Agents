"""Reader for HistData.com's free 1-minute FX files - RESEARCH ONLY
(Phase 103, D123).

**Why this exists.** The IG API key is suspended, so no FX bar can be
fetched from the venue this platform would trade FX at. HistData.com
publishes free annual 1-minute bar files per pair ("Free Forex Historical
Data"); its FAQ states the data is free, carries no warranty, is stamped
in EST WITHOUT daylight saving, is built from the BID price, and has no
volume. Its robots.txt disallows only `/wp-admin/`. The two files used for
D123's research (EUR/USD 2024 and 2025) were downloaded once, by hand, via
the site's own download form - which is what this module expects as input.

**What this module deliberately does NOT do:**

* **It makes no network call.** It reads a local CSV the operator already
  has. Automating HistData's download form is not something this platform
  does; the site sells bulk/automatic delivery as a separate service.
* **It is not wired into `BarBackfillRouter`**, and bars from it are not
  written to `market_data_bars` by any code path. HistData does not grant
  redistribution rights in anything published on the site, so the files
  and anything derived bar-for-bar from them stay on the operator's disk
  and out of the repository and the shared database.

**The two properties that matter for a backtest, stated plainly:**

1. **Bid, not mid.** Every price is the bid. A bid series is the mid
   shifted down by half the spread; for a round trip priced by
   `FxSpreadCostModel` (which charges a full spread on top) the constant
   part of that shift cancels, but stops and targets are TRIGGERED on bid
   highs/lows, so a long's stop fires marginally early and a short's
   marginally late relative to a mid series. `source="histdata-bid"`
   carries this onto every bar.
2. **Fixed UTC-5 timestamps.** EST without DST is exactly UTC-05:00 all
   year, so conversion is a fixed offset - NOT America/New_York, which
   would shift half the year by an hour.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from apps.api.app.marketdata.bar_provider import Bar

HISTDATA_TZ = timezone(timedelta(hours=-5), name="EST-noDST")
SOURCE = "histdata-bid"


class HistDataFormatError(ValueError):
    """A line that is not HistData's `YYYYMMDD HHMMSS;O;H;L;C;V` format."""


def parse_line(line: str, *, symbol: str) -> Bar:
    parts = line.strip().split(";")
    if len(parts) != 6:
        raise HistDataFormatError(f"expected 6 ';'-separated fields, got {line!r}")
    try:
        local = datetime.strptime(parts[0], "%Y%m%d %H%M%S").replace(tzinfo=HISTDATA_TZ)
        o, h, low, c = (Decimal(x) for x in parts[1:5])
    except (ValueError, InvalidOperation) as exc:
        raise HistDataFormatError(f"unreadable HistData line {line!r}") from exc
    if c <= 0:
        raise HistDataFormatError(f"non-positive close in {line!r}")
    return Bar(
        symbol=symbol,
        bar_interval="1m",
        ts=local.astimezone(UTC),
        open=o,
        high=h,
        low=low,
        close=c,
        # HistData's sixth column is always 0 for FX: there is no volume.
        volume=None,
        source=SOURCE,
    )


def iter_bars(lines: Iterable[str], *, symbol: str) -> Iterator[Bar]:
    for line in lines:
        if line.strip():
            yield parse_line(line, symbol=symbol)


def load_m1_file(path: str | Path, *, symbol: str) -> list[Bar]:
    """Every 1-minute bar in one HistData ASCII M1 CSV, oldest first."""
    with open(path, encoding="utf-8") as handle:
        bars = list(iter_bars(handle, symbol=symbol))
    bars.sort(key=lambda b: b.ts)
    return bars
