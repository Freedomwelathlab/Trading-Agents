"""IG historical prices -> mid-price FX bars and a spread series
(Phase 103, D123).

**Built against IG's documented REST API, not against a live response.**
The IG demo API key this platform was given is SUSPENDED by IG
(`error.security.client-suspended`, 2026-09-25; see `execution/adapters/
ig.py`), and every further login with it extends the suspension. So this
provider is exercised only through a stubbed HTTP client in tests, and it
is OFF unless `FX_BAR_PROVIDER=ig` - which nothing in this repository
sets. What is assumed from IG's documentation, and must be checked on the
first real fetch:

* `GET /prices/{epic}?resolution=MINUTE_5&from=...&to=...&pageSize=&
  pageNumber=` (header `Version: 3`) returns `prices[]`, each with
  `snapshotTimeUTC` and `openPrice`/`highPrice`/`lowPrice`/`closePrice`
  as `{bid, ask, lastTraded}`, plus `metadata.pageData.totalPages` and
  `metadata.allowance.remainingAllowance`.
* Currency CFD prices are quoted in IG "points" and `GET /markets/{epic}`
  (Version 3) reports `snapshot.scalingFactor` (10000 for EUR/USD), by
  which every price is divided here. A missing or non-positive factor is
  REFUSED rather than assumed to be 1 or 10000 - guessing it wrong would
  store every bar four orders of magnitude off.
* `from`/`to` are sent as naive `yyyy-MM-ddTHH:mm:ss`; the timestamps
  stored are the response's `snapshotTimeUTC`, never the request's, so an
  ambiguity in how IG reads the window moves its edges by hours but can
  never mis-stamp a bar.

**Mid bars and a spread series, never a bid or ask bar.** Each stored bar
is the bid/ask average per field. For open/close that is exactly the mid
at that instant; for high/low it is the average of the bid extreme and the
ask extreme, which equals the true mid extreme only when the spread was
constant across the bar - documented, not hidden. The close-to-close
spread (`ask - bid`) is returned beside the bars and summarised in pips on
every fetch, so the cost model's per-pair defaults can be replaced with
what IG actually quoted. It is not persisted: that needs a table
(migration 0038 is reserved for it) and is deferred - see D123.

**`volume` is always None.** IG's `lastTradedVolume` on a currency CFD is
a tick count, not traded size (see `marketdata/fx.py`).

**Historical allowance.** IG caps historical price points per API key per
week (10,000 at the time of writing). A 5-minute bar is one point, so a
month of 5-minute EUR/USD (~6,000 bars) is most of a week's allowance and
the 180-day windows the equity research used are NOT fetchable in one go.
An exhausted allowance surfaces as IG's own error code through
`VendorError`, and the backfill job records it as FAILED.
"""

from __future__ import annotations

import asyncio
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from apps.api.app.core.logging import get_logger
from apps.api.app.execution.adapters.ig import IgAdapter, IgError, build_ig_adapter
from apps.api.app.execution.env_credentials import load_env_credentials
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.fx import (
    DEFAULT_IG_EPIC_TEMPLATE,
    FX_PAIRS,
    fx_pair,
    ig_epic_for,
)
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError

logger = get_logger(__name__)

IG_RESOLUTIONS: dict[str, str] = {
    "1m": "MINUTE",
    "5m": "MINUTE_5",
    "15m": "MINUTE_15",
    "30m": "MINUTE_30",
    "1h": "HOUR",
}
"""Our `BarInterval` -> IG's `resolution`. `1d` is absent on purpose: an
FX day ends at the 17:00 New York roll, and a vendor's DAY bar with an
unverified boundary would be stored under a label that may not describe
it."""

PAGE_SIZE = 1000
MAX_PAGES = 200
"""A runaway guard, not a data limit: 200,000 bars is far beyond any
single week's allowance, so reaching it means pagination is misbehaving."""


@dataclass
class FxBarSeries:
    """What one IG fetch produced: mid bars, the close spread per bar, and
    how many price rows were skipped for lacking a bid or ask close."""

    symbol: str
    bar_interval: str
    bars: list[Bar] = field(default_factory=list)
    spreads: list[tuple[datetime, Decimal]] = field(default_factory=list)
    skipped_rows: int = 0
    remaining_allowance: int | None = None

    def spread_pips_summary(self, pip_size: Decimal) -> dict[str, Decimal] | None:
        """Median / 90th percentile / max close spread in pips, or None when
        no spread was observed. Quantiles of what IG quoted, not a model."""
        if not self.spreads or pip_size <= 0:
            return None
        pips = sorted(float(s / pip_size) for _, s in self.spreads)
        p90 = pips[min(len(pips) - 1, int(0.9 * len(pips)))]
        return {
            "median": Decimal(str(round(statistics.median(pips), 3))),
            "p90": Decimal(str(round(p90, 3))),
            "max": Decimal(str(round(pips[-1], 3))),
        }


def _dec(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value.is_finite() else None


def _side(price: Any, side: str) -> Decimal | None:
    if not isinstance(price, Mapping):
        return None
    return _dec(price.get(side))


def _mid(price: Any, scale: Decimal) -> Decimal | None:
    bid, ask = _side(price, "bid"), _side(price, "ask")
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2 / scale


def _parse_utc(raw: Any) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise VendorError(
            f"IG price row has no snapshotTimeUTC ({raw!r}). Version 3 of /prices returns "
            "it; without it a bar cannot be placed in time, so nothing is stored."
        )
    text = raw.replace("/", "-").replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise VendorError(f"IG returned an unreadable snapshotTimeUTC: {raw!r}") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class IgFxBarProvider:
    """`HistoricalBarProvider`-shaped FX bars from IG's /prices endpoint."""

    name = "ig"

    def __init__(
        self, adapter: IgAdapter, *, epic_template: str = DEFAULT_IG_EPIC_TEMPLATE
    ) -> None:
        self._adapter = adapter
        self._epic_template = epic_template

    def _scaling_factor(self, epic: str) -> Decimal:
        body = self._adapter.market_data_get(f"/markets/{epic}", version="3")
        snapshot = body.get("snapshot")
        factor = _dec(snapshot.get("scalingFactor")) if isinstance(snapshot, Mapping) else None
        if factor is None or factor <= 0:
            raise VendorError(
                f"IG returned no usable snapshot.scalingFactor for {epic} ({factor!r}). "
                "Prices are refused rather than stored at a guessed scale."
            )
        return factor

    def fetch(
        self, symbol: str, *, bar_interval: str, start_date: date, end_date: date
    ) -> FxBarSeries:
        """Synchronous fetch. Raises `ValueError` for a request this
        provider cannot express, `VendorError` for anything IG refused or
        returned unreadably, `DataUnavailableError` for an empty window."""
        resolution = IG_RESOLUTIONS.get(bar_interval)
        if resolution is None:
            raise ValueError(
                f"IG FX bars support {sorted(IG_RESOLUTIONS)}; got {bar_interval!r}. "
                "Daily FX bars are refused rather than stored with an unverified day boundary."
            )
        epic = ig_epic_for(symbol, self._epic_template)
        series = FxBarSeries(symbol=symbol, bar_interval=bar_interval)
        try:
            scale = self._scaling_factor(epic)
            query = {
                "resolution": resolution,
                "from": datetime.combine(start_date, time(0, 0)).strftime("%Y-%m-%dT%H:%M:%S"),
                "to": datetime.combine(end_date, time(23, 59, 59)).strftime("%Y-%m-%dT%H:%M:%S"),
                "pageSize": PAGE_SIZE,
            }
            page = 1
            while True:
                if page > MAX_PAGES:
                    raise VendorError(
                        f"IG pagination for {epic} exceeded {MAX_PAGES} pages; refusing to "
                        "continue a fetch whose paging no longer makes sense."
                    )
                body = self._adapter.market_data_get(
                    f"/prices/{epic}?{urlencode({**query, 'pageNumber': page})}", version="3"
                )
                self._absorb(series, body, scale)
                metadata = body.get("metadata")
                page_data = metadata.get("pageData") if isinstance(metadata, Mapping) else None
                total = page_data.get("totalPages") if isinstance(page_data, Mapping) else None
                if not isinstance(total, int) or page >= total:
                    break
                page += 1
        except IgError as exc:
            raise VendorError(str(exc)) from exc

        if not series.bars:
            raise DataUnavailableError(
                f"IG returned no priced bars for {epic} {bar_interval} between "
                f"{start_date.isoformat()} and {end_date.isoformat()}"
                + (f" ({series.skipped_rows} rows lacked a bid or ask)." if series.skipped_rows
                   else ".")
            )
        series.bars.sort(key=lambda b: b.ts)
        series.spreads.sort(key=lambda p: p[0])
        return series

    def _absorb(self, series: FxBarSeries, body: Mapping[str, Any], scale: Decimal) -> None:
        metadata = body.get("metadata")
        if isinstance(metadata, Mapping):
            allowance = metadata.get("allowance")
            if isinstance(allowance, Mapping) and isinstance(
                allowance.get("remainingAllowance"), int
            ):
                series.remaining_allowance = allowance["remainingAllowance"]
        rows = body.get("prices")
        if not isinstance(rows, list):
            raise VendorError(f"IG /prices returned no prices array: {sorted(body)!r}")
        for row in rows:
            if not isinstance(row, Mapping):
                series.skipped_rows += 1
                continue
            close = _mid(row.get("closePrice"), scale)
            if close is None or close <= 0:
                # A bar with no two-sided close is not a mid bar. Skipped and
                # counted, never filled from the bid alone.
                series.skipped_rows += 1
                continue
            ts = _parse_utc(row.get("snapshotTimeUTC"))
            series.bars.append(
                Bar(
                    symbol=series.symbol,
                    bar_interval=series.bar_interval,
                    ts=ts,
                    open=_q(_mid(row.get("openPrice"), scale)),
                    high=_q(_mid(row.get("highPrice"), scale)),
                    low=_q(_mid(row.get("lowPrice"), scale)),
                    close=_q(close) or close,
                    volume=None,
                    source="ig-mid",
                )
            )
            bid = _side(row.get("closePrice"), "bid")
            ask = _side(row.get("closePrice"), "ask")
            if bid is not None and ask is not None:
                series.spreads.append((ts, (ask - bid) / scale))

    async def get_bars(
        self, symbol: str, *, bar_interval: str, start_date: date, end_date: date
    ) -> list[Bar]:
        series = await asyncio.to_thread(
            self.fetch, symbol, bar_interval=bar_interval, start_date=start_date,
            end_date=end_date,
        )
        spec = FX_PAIRS.get(fx_pair(symbol))
        summary = series.spread_pips_summary(spec.pip_size) if spec else None
        logger.info(
            "fx_bars_fetched",
            symbol=symbol,
            bar_interval=bar_interval,
            bars=len(series.bars),
            skipped_rows=series.skipped_rows,
            remaining_allowance=series.remaining_allowance,
            spread_pips_median=str(summary["median"]) if summary else None,
            spread_pips_p90=str(summary["p90"]) if summary else None,
        )
        return series.bars


_STORE_PRECISION = Decimal("0.000001")


def _q(value: Decimal | None) -> Decimal | None:
    """Quantize to the store's NUMERIC(20,6). A mid of two 5-decimal
    quotes has at most six decimals, so this is exact for every major."""
    return None if value is None else value.quantize(_STORE_PRECISION)


def build_ig_fx_bar_provider(
    settings: Any, environ: Mapping[str, str] | None = None
) -> IgFxBarProvider | None:
    """The FX leg of `BarBackfillRouter`, or None.

    Two gates, both required: `FX_BAR_PROVIDER=ig` (default `none`) AND a
    complete `IG_*` credential set in the environment. Building makes NO
    network call - `IgAdapter` logs in lazily on its first request - so
    enabling this cannot, by itself, spend a login attempt against a
    suspended key.
    """
    if getattr(settings, "fx_bar_provider", "none") != "ig":
        return None
    credentials = load_env_credentials("ig", environ)
    if credentials is None:
        logger.warning(
            "fx_bar_provider_not_configured",
            reason="FX_BAR_PROVIDER=ig but the IG_* credential set is incomplete",
        )
        return None
    try:
        adapter = build_ig_adapter(credentials)
    except IgError as exc:
        logger.warning("fx_bar_provider_not_configured", reason=str(exc))
        return None
    return IgFxBarProvider(
        adapter,
        epic_template=getattr(settings, "fx_ig_epic_template", DEFAULT_IG_EPIC_TEMPLATE),
    )
