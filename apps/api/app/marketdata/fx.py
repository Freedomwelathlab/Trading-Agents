"""Spot FX instruments: symbols, pip sizes, venue mapping, resampling
(Phase 103, D123).

**What an FX bar in this system is, and is not.** Spot FX is an
over-the-counter market with no consolidated tape, so three things every
equity bar has are simply absent, and the code refuses to pretend
otherwise:

* **No volume.** There is no traded-volume figure for spot EUR/USD. What
  venues publish in that slot is a tick COUNT (IG's `lastTradedVolume` on a
  currency CFD, HistData's zero column) - a measure of quote activity at
  one dealer, not of size traded. Storing it as `volume` would let the
  volume-gated setups and session VWAP read it as real volume, so every FX
  bar here carries `volume=None`. Those setups then refuse, which is the
  correct answer (D123 verifies it).
* **No last-trade price.** Bars are built from quotes. Mid bars (IG) are
  the average of bid and ask; bid bars (HistData) are the bid alone. The
  `source` string says which, because a bid series sits half a spread
  below the mid series of the same market.
* **No exchange session.** See `sessions.FxCalendar`.

**Symbol convention: `EURUSD.FX`.** Six letters, base then quote, and the
`.FX` suffix. The suffix is what `BarBackfillRouter` routes on, and it is
chosen so it can never collide with a Longbridge market suffix (`.US`,
`.HK`, `.SG`, `.SH`, `.SZ`) or a Coinbase `BASE-QUOTE` product id.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.sessions import (
    FX_CALENDAR,
    US_EQUITY_CALENDAR,
    SessionCalendar,
)

FX_SUFFIX = ".FX"
_FX_RE = re.compile(r"^([A-Z]{3})([A-Z]{3})\.FX$")


class NotAnFxSymbolError(ValueError):
    """The symbol is not in the `EURUSD.FX` convention."""


def is_fx_symbol(symbol: str) -> bool:
    return bool(_FX_RE.match((symbol or "").strip().upper()))


def fx_pair(symbol: str) -> str:
    """`EURUSD.FX` -> `EURUSD`, or a refusal naming the convention."""
    match = _FX_RE.match((symbol or "").strip().upper())
    if match is None:
        raise NotAnFxSymbolError(
            f"{symbol!r} is not an FX symbol. FX symbols are six letters, base then "
            "quote, with a .FX suffix - e.g. 'EURUSD.FX'."
        )
    return match.group(1) + match.group(2)


@dataclass(frozen=True)
class FxPairSpec:
    """Per-pair constants the cost model needs.

    `default_half_spread_pips` is an ASSUMPTION, not a measurement. The
    values below are roughly IG's advertised typical (not minimum) spreads
    for its standard CFD account, halved, and rounded UP - IG advertises
    EUR/USD from 0.6 pips, and a backtest that assumes the minimum spread is
    assuming the best minute of the day all day. They are defaults to be
    replaced by `IgFxBarProvider`'s observed spread series once IG data can
    be fetched (the key is suspended as of D123), and every research run
    prints the figure it used.
    """

    pair: str
    pip_size: Decimal
    default_half_spread_pips: Decimal


FX_PAIRS: dict[str, FxPairSpec] = {
    spec.pair: spec
    for spec in (
        FxPairSpec("EURUSD", Decimal("0.0001"), Decimal("0.5")),
        FxPairSpec("GBPUSD", Decimal("0.0001"), Decimal("0.75")),
        FxPairSpec("AUDUSD", Decimal("0.0001"), Decimal("0.5")),
        FxPairSpec("NZDUSD", Decimal("0.0001"), Decimal("0.75")),
        FxPairSpec("USDCAD", Decimal("0.0001"), Decimal("0.75")),
        FxPairSpec("USDCHF", Decimal("0.0001"), Decimal("0.75")),
        FxPairSpec("EURGBP", Decimal("0.0001"), Decimal("0.75")),
        FxPairSpec("USDJPY", Decimal("0.01"), Decimal("0.5")),
        FxPairSpec("EURJPY", Decimal("0.01"), Decimal("1.0")),
        FxPairSpec("GBPJPY", Decimal("0.01"), Decimal("1.5")),
    )
}


def pair_spec(symbol: str) -> FxPairSpec:
    """The spec for a known pair. An unknown pair is REFUSED rather than
    given a guessed spread: the cost is the whole question for intraday FX,
    and a made-up default would decide the result."""
    pair = fx_pair(symbol)
    spec = FX_PAIRS.get(pair)
    if spec is None:
        raise NotAnFxSymbolError(
            f"No cost defaults for {pair}; known pairs are {sorted(FX_PAIRS)}. Pass an "
            "explicit half-spread rather than letting the backtest guess one."
        )
    return spec


def calendar_for_symbol(symbol: str) -> SessionCalendar:
    """The session clock a symbol trades on: FX for `.FX`, else US equity.

    Crypto (`BTC-USD`) keeps the equity default here because that is what
    every existing caller has always used for it; it is not a claim that
    crypto trades US equity hours.
    """
    return FX_CALENDAR if is_fx_symbol(symbol) else US_EQUITY_CALENDAR


DEFAULT_IG_EPIC_TEMPLATE = "CS.D.{pair}.MINI.IP"
"""IG's mini-CFD epic shape for the majors, e.g. `CS.D.EURUSD.MINI.IP`.
Accounts differ (a spread-bet account uses `.TODAY.IP`, a full CFD
`.CFD.IP`), which is why the template is a setting and not a constant."""


def ig_epic_for(symbol: str, template: str = DEFAULT_IG_EPIC_TEMPLATE) -> str:
    return template.format(pair=fx_pair(symbol))


_RESAMPLE_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60}


def resample_bars(
    bars: Sequence[Bar], bar_interval: str, *, symbol: str | None = None
) -> list[Bar]:
    """Aggregate finer bars into `bar_interval` buckets, aligned to UTC.

    Open is the first bar's open, close the last bar's close, high/low the
    extremes - the standard OHLC aggregation, over real bars only. A bucket
    is emitted only if at least one real bar fell in it; a quiet minute
    that a vendor did not print is not filled in, so a bucket with fewer
    sub-bars is still exactly the prices that traded in it. Volume stays
    `None` unless EVERY sub-bar carried one.

    UTC alignment is correct for FX: 17:00 New York is always a whole UTC
    hour, so no bucket straddles the daily roll. `1d` is refused - a daily
    FX bar needs the 17:00 NY roll as its boundary, not UTC midnight.
    """
    minutes = _RESAMPLE_MINUTES.get(bar_interval)
    if minutes is None:
        raise ValueError(
            f"resample_bars supports {sorted(_RESAMPLE_MINUTES)}; got {bar_interval!r}."
        )
    span = timedelta(minutes=minutes)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    out: list[Bar] = []
    current_key: datetime | None = None
    bucket: list[Bar] = []

    def flush() -> None:
        if not bucket or current_key is None:
            return
        highs = [b.high if b.high is not None else b.close for b in bucket]
        lows = [b.low if b.low is not None else b.close for b in bucket]
        volumes = [b.volume for b in bucket]
        out.append(
            Bar(
                symbol=symbol or bucket[0].symbol,
                bar_interval=bar_interval,
                ts=current_key,
                open=bucket[0].open if bucket[0].open is not None else bucket[0].close,
                high=max(highs),
                low=min(lows),
                close=bucket[-1].close,
                volume=None if any(v is None for v in volumes) else sum(v or 0 for v in volumes),
                source=bucket[0].source,
            )
        )

    for bar in sorted(bars, key=lambda b: b.ts):
        ts = bar.ts.astimezone(UTC)
        key = epoch + ((ts - epoch) // span) * span
        if key != current_key:
            flush()
            current_key = key
            bucket = []
        bucket.append(bar)
    flush()
    return out
