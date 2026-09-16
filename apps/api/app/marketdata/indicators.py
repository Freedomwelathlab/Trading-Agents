"""Deterministic technical indicators - pure functions, no LLM, no I/O, no
DB (docs/TOKEN_POLICY.md's mandatory-deterministic list explicitly names
RSI/SMA; D019/D021 exist specifically to keep that rule true once real
historical data became available). An LLM (TechnicalAnalyst) may narrate
a value these functions computed - it must never compute one itself.
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol


class InsufficientDataError(Exception):
    """Not enough closes to compute the requested indicator. Never
    padded, never approximated from fewer points than the definition
    requires - the caller must treat this like any other DATA_UNAVAILABLE
    case, not a reason to guess."""


def sma(closes: list[Decimal], period: int) -> Decimal:
    """Simple moving average of the most recent `period` closes.
    `closes` must be oldest-first (HistoryProvider's contract)."""
    if period < 1:
        raise ValueError("period must be at least 1")
    if len(closes) < period:
        raise InsufficientDataError(
            f"SMA({period}) needs {period} closes, got {len(closes)}."
        )
    window = closes[-period:]
    return sum(window, Decimal(0)) / Decimal(period)


def rsi(closes: list[Decimal], period: int = 14) -> Decimal:
    """Relative Strength Index over `period` (Wilder's original, simple-
    average variant - not the smoothed/exponential refinement some
    platforms use; this is the textbook definition, not a choice to
    match any specific vendor's charting). `closes` must be oldest-first.
    Needs period + 1 closes (period *changes* between them)."""
    if period < 1:
        raise ValueError("period must be at least 1")
    if len(closes) < period + 1:
        raise InsufficientDataError(
            f"RSI({period}) needs {period + 1} closes, got {len(closes)}."
        )

    window = closes[-(period + 1) :]
    gains = Decimal(0)
    losses = Decimal(0)
    # window has period+1 closes; window[1:] is naturally one shorter -
    # that's the intended pairing (each close with its predecessor), not
    # a length mismatch to guard against, so strict=True would be wrong here.
    for prev, curr in zip(window, window[1:], strict=False):
        change = curr - prev
        if change > 0:
            gains += change
        else:
            losses += -change

    avg_gain = gains / Decimal(period)
    avg_loss = losses / Decimal(period)

    if avg_loss == 0:
        return Decimal(100) if avg_gain > 0 else Decimal(50)

    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


def ema(closes: list[Decimal], period: int) -> Decimal:
    """Exponential moving average of the most recent `period` closes,
    smoothing factor `2 / (period + 1)`. `closes` must be oldest-first.

    **Seeded from the SMA of the first `period` closes**, then stepped
    forward one close at a time. That seeding choice is the conventional
    one and it matters: an EMA seeded from a single close instead would
    make the value depend heavily on where the caller happened to start
    the series, so two callers passing different amounts of history for
    the same bar would get different "EMA(20)"s. Seeding from the SMA
    still leaves a mild dependence on series length - that is inherent to
    EMA and is not smoothed over here - but it converges quickly and is
    what every charting package this is likely to be compared against
    does.

    Needs exactly `period` closes, like `sma` and unlike `rsi`: an EMA
    measures levels, not changes, so there is no extra bar to pay for.
    """
    if period < 1:
        raise ValueError("period must be at least 1")
    if len(closes) < period:
        raise InsufficientDataError(
            f"EMA({period}) needs {period} closes, got {len(closes)}."
        )

    multiplier = Decimal(2) / Decimal(period + 1)
    value = sum(closes[:period], Decimal(0)) / Decimal(period)
    for close in closes[period:]:
        value = (close - value) * multiplier + value
    return value


class _OHLCBar(Protocol):
    """The three fields `atr` reads. A structural Protocol rather than an
    import of marketdata.bar_provider.Bar, so this module stays what its
    docstring says it is - pure arithmetic over numbers, with no dependency
    on the shape a vendor adapter happens to produce. Any object carrying
    these three attributes satisfies it, which is what makes the ATR tests
    hand-writable from a table of numbers.

    All three are `Decimal | None` because `market_data_bars` stores
    `high`/`low` as nullable: a vendor may in principle return a
    close-only record, and Bar's own docstring says so.
    """

    @property
    def high(self) -> Decimal | None: ...
    @property
    def low(self) -> Decimal | None: ...
    @property
    def close(self) -> Decimal | None: ...


def atr(bars: Sequence[_OHLCBar], period: int = 14) -> Decimal:
    """Average True Range over `period` - the simple average of the most
    recent `period` true ranges. `bars` must be oldest-first.

    True Range for a bar is `max(high - low, |high - prev_close|,
    |low - prev_close|)`. The two terms involving the previous close are
    what make ATR a volatility measure rather than a bar-height measure:
    they capture a gap between sessions, which a plain high-minus-low
    misses entirely.

    **The simple-average variant, not Wilder's smoothing** - the same
    choice `rsi` above documents, made the same way and for the same
    reason: one convention across this module, stated rather than
    inherited from whichever platform a reader happens to know. Wilder's
    smoothed ATR is a different number and callers should not assume
    either one matches a particular vendor's chart.

    Needs `period + 1` bars: the first true range needs a bar before it to
    take a previous close from, exactly as RSI needs `period + 1` closes to
    measure `period` changes.

    Raises `InsufficientDataError` - the same error, not a second one
    meaning the same thing - when a bar in the window is missing `high`,
    `low`, or `close`. A bar without a high and a low genuinely cannot
    produce a true range, and callers already treat this error as "no
    answer here" (expressions.py records `None` and the rule does not
    fire). Inventing a high from the close, which is the only alternative,
    would be fabricating market data.
    """
    if period < 1:
        raise ValueError("period must be at least 1")
    if len(bars) < period + 1:
        raise InsufficientDataError(
            f"ATR({period}) needs {period + 1} bars, got {len(bars)}."
        )

    window = list(bars[-(period + 1) :])
    total = Decimal(0)
    # window has period+1 bars; each true range pairs a bar with its
    # predecessor, so window[1:] yields exactly `period` of them.
    for prev, curr in zip(window, window[1:], strict=False):
        if curr.high is None or curr.low is None or curr.close is None:
            raise InsufficientDataError(
                f"ATR({period}) needs high/low/close on every bar in the window, but a "
                "bar is missing at least one of them. A true range cannot be computed "
                "from a close-only bar, and one is never estimated."
            )
        if prev.close is None:
            raise InsufficientDataError(
                f"ATR({period}) needs the previous bar's close to measure a gap, but "
                "that bar has none."
            )
        total += max(
            curr.high - curr.low,
            abs(curr.high - prev.close),
            abs(curr.low - prev.close),
        )

    return total / Decimal(period)
