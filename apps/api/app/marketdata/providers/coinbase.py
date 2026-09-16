"""Coinbase Exchange public market data - real 24/7 BTC/USD OHLCV bars
(Phase 71, docs/DECISIONS.md D089).

**Why a second vendor at all.** The Longbridge account this platform is
configured against answers `301600 invalid symbol` for `BTCUSD.BKKT` and
returns nothing for `.HAS`/`.OSL` crypto symbols: it can price US equities
and Bitcoin ETFs (IBIT, GBTC, BITO, MSTR) but has no spot BTC/USD
instrument at all. The BTC/USD strategy this provider exists to feed
therefore cannot be built on Longbridge data, and no amount of
re-authorizing changes that - it is an entitlement fact, not a token fact.

**Why Coinbase specifically**, measured rather than assumed, against the
three candidates:

  * Kraken's public OHLC endpoint IGNORES `since` when asked to go
    backwards - it returns the most recent 720 bars whatever you pass - so
    hourly history reaches back only ~30 days. The spec's 90- and 180-day
    windows are simply not obtainable from it.
  * Binance exposes a real `BTCUSD` pair, but it is nearly untraded:
    median 0.04 BTC per hour against BTCUSDT's 560, and daily history
    beginning four days ago. Backtesting it would produce numbers about an
    illiquid book rather than about Bitcoin. Its liquid pair is BTC/USDT,
    which is a tether pair, not a dollar pair.
  * Coinbase `BTC-USD` is a genuine, deeply liquid USD market - median 218
    BTC per hour in the sampled window - with real `start`/`end` range
    support, and 5-minute candles still available 180 days back.

So Coinbase is the only one of the three that answers the actual
requirement: real dollars, real depth, and the windows the spec asks for.

**No credentials.** This uses the public, unauthenticated Exchange data
API, so unlike every other provider in this package there is no
all-or-nothing credential gate and no `build_*` returning `None`. That is
also why it can never place an order: this module is market data only, and
execution stays with the broker adapters.

**The response shape is unusual and is handled explicitly.** Coinbase
returns bare arrays ordered NEWEST FIRST as
`[time, low, high, open, close, volume]` - note `low` and `high` precede
`open`, which is not the OHLCV ordering every other vendor in this
codebase uses. Reading those positionally in the obvious order would
silently transpose every bar's open with its low. The mapping below is
written out field by field for exactly that reason, and the bars are
re-sorted oldest-first to satisfy `HistoricalBarProvider`'s contract.
"""

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from apps.api.app.core.logging import get_logger
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError

logger = get_logger(__name__)

_BASE_URL = "https://api.exchange.coinbase.com"

MAX_CANDLES_PER_REQUEST = 300
"""Coinbase's hard per-request cap. Requesting a wider window does not
return more - it returns an error - so the fetch below pages rather than
asking for everything at once."""

_GRANULARITY_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "1d": 86400,
}
"""Our `BarInterval` vocabulary -> Coinbase's `granularity` in seconds.

Coinbase supports exactly {60, 300, 900, 3600, 21600, 86400} and rejects
anything else. Two consequences are deliberate and documented rather than
papered over:

  * `30m` is ABSENT. Coinbase has no 30-minute granularity, and the
    alternatives - silently serving 15m or 1h bars under a `30m` label, or
    resampling two 15m bars into one - would both store bars whose
    `bar_interval` lies about what they are. A caller asking for `30m` gets
    a refusal naming what is available.
  * Coinbase's 6-hour granularity (21600) has no entry because `6h` is not
    in `BarInterval`; what this system can ingest never outruns what it can
    store and evaluate.
"""

_REQUEST_TIMEOUT_SECONDS = 20
_PAGE_PAUSE_SECONDS = 0.35
"""Courtesy pause between pages. Coinbase's public rate limit is ~10
requests/second per IP; a 180-day hourly backfill is 15 pages, so this
costs about five seconds in total and keeps a large backfill from looking
like abuse."""


def _granularity(bar_interval: str) -> int:
    """Coinbase's `granularity` for one of our intervals, or a refusal.

    Raises `ValueError` rather than falling back to the nearest supported
    granularity. A fallback would persist (say) hourly bars under a
    `bar_interval` of `"30m"`, and nothing downstream - backtest,
    deployment, chart - could detect that the bars were not what their own
    label said. That is fabricated market data in the form hardest to
    catch.
    """
    seconds = _GRANULARITY_SECONDS.get(bar_interval)
    if seconds is None:
        supported = ", ".join(sorted(_GRANULARITY_SECONDS))
        raise ValueError(
            f"CoinbaseBarProvider cannot serve bar_interval {bar_interval!r} - Coinbase "
            f"supports {supported}. It has no 30-minute granularity, and this provider "
            "refuses rather than substituting a different one under that label."
        )
    return seconds


def _decimal(value: Any) -> Decimal:
    """Through `str` on purpose: Coinbase sends JSON numbers, and
    `Decimal(0.1)` is the binary float 0.1000000000000000055..., not the
    price the exchange printed."""
    return Decimal(str(value))


class CoinbaseBarProvider:
    """Real OHLCV bars for a Coinbase product, implementing
    `HistoricalBarProvider`.

    `symbol` is a Coinbase product id (`BTC-USD`, `ETH-USD`), used verbatim
    - this provider does not translate symbols. A caller that wants
    Longbridge's `AAPL.US` spelling is asking the wrong provider, and
    guessing a translation would be how a US equity quietly gets priced off
    a crypto exchange.
    """

    name = "coinbase"

    def __init__(self, *, base_url: str = _BASE_URL) -> None:
        self._base_url = base_url.rstrip("/")

    async def get_bars(
        self, symbol: str, *, bar_interval: str, start_date: date, end_date: date
    ) -> list[Bar]:
        """Real bars in `[start_date, end_date]`, oldest first.

        Pages forward in windows of at most `MAX_CANDLES_PER_REQUEST`
        candles. `end_date` is inclusive and treated as the whole UTC day,
        matching `MarketDataStore.get_bars`' own range semantics - a caller
        asking for "through the 30th" means through the end of the 30th.

        Returns fewer bars than the window spans whenever the exchange has
        fewer, and never pads: a venue outage is a real gap, and
        `HistoricalBarProvider`'s contract is explicit that a short result
        must be treated as one.

        Raises `DataUnavailableError` when the whole window yields nothing
        (an unknown product, or a range entirely before the product
        listed), and `VendorError` when the exchange itself fails - the two
        are different problems and the caller is told which.
        """
        granularity = _granularity(bar_interval)

        window_start = datetime.combine(start_date, datetime.min.time(), tzinfo=UTC)
        window_end = datetime.combine(end_date, datetime.max.time(), tzinfo=UTC)

        # De-duplicated by timestamp: consecutive pages can overlap at their
        # boundary, and `market_data_bars`' composite primary key would
        # reject a duplicate on write rather than quietly ignoring it.
        by_ts: dict[datetime, Bar] = {}
        span = timedelta(seconds=granularity * MAX_CANDLES_PER_REQUEST)
        cursor = window_start
        pages = 0

        while cursor <= window_end:
            page_end = min(cursor + span, window_end)
            rows = await self._fetch_page(symbol, granularity, cursor, page_end)
            pages += 1
            for row in rows:
                bar = self._row_to_bar(symbol, bar_interval, row)
                if bar is not None and window_start <= bar.ts <= window_end:
                    by_ts[bar.ts] = bar
            cursor = page_end + timedelta(seconds=granularity)
            if cursor <= window_end:
                await asyncio.sleep(_PAGE_PAUSE_SECONDS)

        if not by_ts:
            raise DataUnavailableError(
                f"Coinbase returned no {bar_interval} candles for {symbol!r} between "
                f"{start_date.isoformat()} and {end_date.isoformat()} across {pages} "
                "request(s). The product id may be wrong, or the range may predate its "
                "listing."
            )

        bars = sorted(by_ts.values(), key=lambda b: b.ts)
        logger.info(
            "coinbase_bars_fetched",
            symbol=symbol,
            bar_interval=bar_interval,
            pages=pages,
            bars=len(bars),
            first=bars[0].ts.isoformat(),
            last=bars[-1].ts.isoformat(),
        )
        return bars

    async def _fetch_page(
        self, symbol: str, granularity: int, start: datetime, end: datetime
    ) -> list[list[Any]]:
        """One page of candles. Blocking I/O is pushed to a worker thread so
        this coroutine never stalls the event loop - the same reason the
        Longbridge adapters await the vendor SDK rather than calling it
        synchronously."""
        query = urllib.parse.urlencode(
            {
                "granularity": granularity,
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": end.isoformat().replace("+00:00", "Z"),
            }
        )
        url = f"{self._base_url}/products/{urllib.parse.quote(symbol)}/candles?{query}"

        def _get() -> list[list[Any]]:
            request = urllib.request.Request(
                url, headers={"User-Agent": "trading-os", "Accept": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
                payload = json.load(response)
            if not isinstance(payload, list):
                # Coinbase reports errors as `{"message": "..."}` with a
                # non-2xx status, but a 200 carrying an object instead of an
                # array would otherwise be iterated as if it were candles.
                raise VendorError(
                    f"Coinbase returned a non-array candle payload for {symbol!r}: "
                    f"{str(payload)[:200]}"
                )
            return payload

        try:
            return await asyncio.to_thread(_get)
        except VendorError:
            raise
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode("utf-8", "replace")
            raise VendorError(
                f"Coinbase candle request failed for {symbol!r} "
                f"({start.date()}..{end.date()}): HTTP {exc.code} {detail}"
            ) from exc
        except Exception as exc:
            raise VendorError(
                f"Coinbase candle request failed for {symbol!r} "
                f"({start.date()}..{end.date()}): {exc}"
            ) from exc

    def _row_to_bar(self, symbol: str, bar_interval: str, row: list[Any]) -> Bar | None:
        """One Coinbase candle array -> one `Bar`, or `None` if unusable.

        **The field order is Coinbase's, not OHLCV**:

            [ time, low, high, open, close, volume ]
              0     1    2     3     4      5

        `low` and `high` come BEFORE `open`. Mapping these positionally in
        the conventional order would transpose every bar's open with its
        low - producing candles that look plausible, validate fine, and are
        wrong in a way no downstream check could catch. Hence the named
        indices below rather than a tuple unpack.

        `time` is the bar's OPEN time in unix seconds, which is what this
        system stores: `engine_v2` evaluates a bar as information available
        at its own timestamp, so a close-stamped bar would shift every
        signal one interval into the future.

        A malformed row is skipped rather than raising, matching the
        Longbridge adapter's handling of a candle with no usable timestamp:
        one bad row in a page is a gap, not a reason to discard the window.
        """
        try:
            ts = datetime.fromtimestamp(int(row[0]), tz=UTC)
            low, high, open_, close, volume = (
                _decimal(row[1]),
                _decimal(row[2]),
                _decimal(row[3]),
                _decimal(row[4]),
                _decimal(row[5]),
            )
        except (IndexError, TypeError, ValueError, ArithmeticError):
            logger.warning("coinbase_candle_unparseable", symbol=symbol, row=str(row)[:120])
            return None

        if close <= 0:
            # `Bar.close` is `Field(gt=0)`; a non-positive close would raise
            # out of the constructor. Skipping keeps one bad print from
            # failing an otherwise good backfill.
            logger.warning("coinbase_candle_non_positive_close", symbol=symbol, close=str(close))
            return None

        return Bar(
            symbol=symbol,
            bar_interval=bar_interval,
            ts=ts,
            open=open_,
            high=high,
            low=low,
            close=close,
            # Coinbase reports base-asset volume as a decimal (0.37 BTC);
            # `market_data_bars.volume` is BIGINT, so a fractional crypto
            # volume truncates to 0. Rounded rather than floored, and the
            # column's integer type is a known limitation recorded in D089
            # rather than something this mapping pretends away.
            volume=int(volume.to_integral_value()),
            source=self.name,
        )


def build_coinbase_bar_provider() -> CoinbaseBarProvider:
    """No credential gate, unlike every `build_longbridge_*` in this
    package: the Coinbase Exchange candle endpoint is public. There is
    therefore no configuration under which this returns `None`, and no
    secret for an operator to set."""
    return CoinbaseBarProvider()
