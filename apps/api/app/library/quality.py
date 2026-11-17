"""A screen for vendor price errors, applied before any history is used.

Measured on Yahoo's daily data (Phase 108): US, Singapore, crypto and FX
series were clean, and Indian series were not. `NIFTYBEES.NS` shows a 90%
fall on 2019-12-19 that is gone two sessions later; `BAJFINANCE.NS` shows a
+10,849% session on 2005-07-28 reversed the next day. Left in, those are a
90% drawdown that never happened and a fortune that was never made.

Two cases, handled differently on purpose:

  * **A spike that comes back.** The price jumps beyond anything the
    market allows, then returns to where it was within a few sessions.
    That is a bad tick. The offending sessions are DROPPED - not repaired,
    not interpolated - and reported by date.
  * **A jump that stays.** The price moves beyond the limit and does not
    return. That may be an unadjusted split or demerger, pre-listing junk
    (`TCS.NS` has rows from two years before its IPO), or it may be real.
    This module cannot tell, so it DISCARDS EVERYTHING BEFORE the jump and
    says so: the symbol simply starts later. A strategy is never handed a
    45% loss or gain that may be fiction. If too little is left, the
    symbol is refused.

Thresholds are deliberately far outside real one-session moves for the
liquid instruments this library is meant for, so a genuine crash is not
mistaken for an error.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from apps.api.app.library.strategies import market_of
from apps.api.app.marketdata.providers.yahoo import DailyHistory

REVERT_WINDOW = 10
"""How many sessions a bad stretch may last before the price must be back."""
REVERT_TOLERANCE = 0.25
"""'Back' means within this fraction of the last good close."""

_LIMITS: dict[str, tuple[float, float]] = {
    "CRYPTO": (-0.60, 1.50),
    "FX": (-0.15, 0.15),
}
_DEFAULT_LIMITS = (-0.35, 0.60)


MIN_BARS_AFTER_SCREEN = 60


class DataQualityError(Exception):
    """Too little trustworthy history is left after the screen."""

    def __init__(self, symbol: str, day: date, move: float) -> None:
        super().__init__(
            f"{symbol} moves {move * 100:+.0f}% in one session on {day.isoformat()} in the "
            "vendor's adjusted series and does not come back - usually an unadjusted split "
            "or demerger - and fewer than "
            f"{MIN_BARS_AFTER_SCREEN} sessions follow it. The symbol is refused."
        )
        self.symbol = symbol
        self.day = day
        self.move = move


_MARKET_ZONES: dict[str, str] = {
    "US": "America/New_York",
    "SG": "Asia/Singapore",
    "IN": "Asia/Kolkata",
    "HK": "Asia/Hong_Kong",
    "AU": "Australia/Sydney",
    "UK": "Europe/London",
}


def market_today(symbol: str, now: datetime) -> date:
    """Today's date where the symbol trades (UTC for crypto and FX)."""
    zone = _MARKET_ZONES.get(market_of(symbol))
    return now.astimezone(ZoneInfo(zone)).date() if zone else now.date()


def completed_sessions(history: DailyHistory, now: datetime) -> DailyHistory:
    """The history without the session still in progress.

    A vendor's daily series includes today's bar while today is still
    trading, and its "close" is just the last price so far. A rule
    evaluated on that would decide on a close that has not happened. So
    any bar dated today (or later) in the market's own timezone is left
    out; it comes back as a completed session tomorrow.
    """
    today = market_today(history.symbol, now)
    keep = sum(1 for d in history.dates if d < today)
    if keep == len(history.dates):
        return history
    return replace(
        history,
        dates=history.dates[:keep],
        closes=history.closes[:keep],
        adj_closes=history.adj_closes[:keep],
    )


def limits_for(symbol: str) -> tuple[float, float]:
    return _LIMITS.get(market_of(symbol), _DEFAULT_LIMITS)


def screen_history(history: DailyHistory) -> DailyHistory:
    """The history with reverting bad ticks dropped and anything before an
    unexplained jump discarded - or a refusal when too little remains."""
    low, high = limits_for(history.symbol)
    adj = history.adj_closes
    n = len(adj)
    keep = [True] * n
    removed: list[date] = []
    start = 0
    truncated: date | None = None
    last_jump = 0.0

    last_good = 0
    i = 1
    while i < n:
        move = adj[i] / adj[last_good] - 1.0
        if low <= move <= high:
            last_good = i
            i += 1
            continue
        base = adj[last_good]
        back = next(
            (
                j
                for j in range(i + 1, min(n, i + 1 + REVERT_WINDOW))
                if abs(adj[j] / base - 1.0) <= REVERT_TOLERANCE
            ),
            None,
        )
        if back is None:
            # Unexplained and permanent: trust nothing before it.
            start = i
            truncated = history.dates[i]
            last_jump = move
            removed = [d for d in removed if d >= truncated]
            last_good = i
            i += 1
            continue
        for k in range(i, back):
            keep[k] = False
            removed.append(history.dates[k])
        last_good = back
        i = back + 1

    if not removed and truncated is None:
        return history
    for k in range(start):
        keep[k] = False
    if truncated is not None and sum(keep) < MIN_BARS_AFTER_SCREEN:
        raise DataQualityError(history.symbol, truncated, last_jump)
    return replace(
        history,
        dates=tuple(d for d, ok in zip(history.dates, keep, strict=True) if ok),
        closes=tuple(v for v, ok in zip(history.closes, keep, strict=True) if ok),
        adj_closes=tuple(v for v, ok in zip(adj, keep, strict=True) if ok),
        removed=tuple(removed),
        truncated_before=truncated,
    )
