"""Deterministic technical indicators - pure functions, no LLM, no I/O, no
DB (docs/TOKEN_POLICY.md's mandatory-deterministic list explicitly names
RSI/SMA; D019/D021 exist specifically to keep that rule true once real
historical data became available). An LLM (TechnicalAnalyst) may narrate
a value these functions computed - it must never compute one itself.
"""

from decimal import Decimal


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
    for prev, curr in zip(window, window[1:]):
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
