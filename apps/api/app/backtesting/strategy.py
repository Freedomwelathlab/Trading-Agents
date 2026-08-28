"""The one, hard-coded v1 strategy: SMA(20) crossover. Pure function, no
LLM, no I/O - same discipline as marketdata/indicators.py, and reuses that
exact module's sma() rather than recomputing a moving average a second way.

Rule (and nothing more - see docs/DECISIONS.md D025 for why this is
deliberately not pluggable/configurable in v1):
  - BUY when yesterday's close was <= SMA(20) and today's close is >
    SMA(20) (price crosses up through the average).
  - SELL when yesterday's close was >= SMA(20) and today's close is <
    SMA(20) (price crosses down through the average).
  - HOLD every other day, including every day before SMA(20) has enough
    history to be defined.

This module only ever proposes a Signal for a given day - it has no notion
of current position, cash, or risk limits. engine.py is responsible for
deciding what (if anything) to actually do with a signal (e.g. a BUY signal
while already holding a position is a no-op, decided by the engine, not
here) and for running every actual trade through the real Risk Engine.
"""

import enum
from decimal import Decimal

from apps.api.app.marketdata.indicators import InsufficientDataError, sma

SMA_PERIOD = 20


class Signal(str, enum.Enum):  # noqa: UP042 (str mixin kept for interop)
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


def generate_signals(closes: list[Decimal], *, period: int = SMA_PERIOD) -> list[Signal]:
    """`closes` must be oldest-first (HistoryProvider's contract). Returns
    one Signal per close, same length and order as `closes`. The first
    `period` entries are always HOLD - not enough history yet for SMA(period)
    to be defined for them, same "never pad, never guess" posture as
    InsufficientDataError itself."""
    n = len(closes)
    signals = [Signal.HOLD] * n
    if n <= period:
        return signals

    # sma_at[i] is SMA(period) computed using closes[0..i] inclusive - None
    # where fewer than `period` closes are available yet.
    sma_at: list[Decimal | None] = [None] * n
    for i in range(period - 1, n):
        try:
            sma_at[i] = sma(closes[: i + 1], period)
        except InsufficientDataError:  # pragma: no cover - unreachable given the loop bound
            sma_at[i] = None

    for i in range(period, n):
        prev_sma = sma_at[i - 1]
        curr_sma = sma_at[i]
        if prev_sma is None or curr_sma is None:
            continue
        prev_close, curr_close = closes[i - 1], closes[i]
        if prev_close <= prev_sma and curr_close > curr_sma:
            signals[i] = Signal.BUY
        elif prev_close >= prev_sma and curr_close < curr_sma:
            signals[i] = Signal.SELL

    return signals
