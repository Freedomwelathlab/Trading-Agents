"""Pure equity-curve math - no LLM, no I/O, deterministic and directly
hand-verifiable (see tests/backtesting/test_metrics.py). Kept separate from
engine.py so each number's correctness can be tested in isolation from the
risk engine / paper broker machinery that produces the inputs."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RoundTrip:
    """One completed buy-then-fully-closing-sell cycle."""

    entry_price: Decimal
    """Quantity-weighted average price of the buy fill(s) that opened this
    position."""
    exit_price: Decimal
    """Price of the sell fill that brought the position back to exactly 0."""


def compute_total_return_pct(*, starting_cash: Decimal, final_equity: Decimal) -> Decimal:
    if starting_cash == 0:
        raise ValueError("starting_cash must be nonzero")
    return (final_equity - starting_cash) / starting_cash * Decimal(100)


def compute_max_drawdown_pct(equity_curve: list[Decimal]) -> Decimal:
    """Largest peak-to-trough decline, as a positive percentage of the peak
    at the time of that trough. Decimal(0) for an empty or single-point
    curve, or a curve that never dips below its running peak."""
    if not equity_curve:
        return Decimal(0)

    peak = equity_curve[0]
    max_drawdown = Decimal(0)
    for equity in equity_curve:
        if equity > peak:
            peak = equity
        if peak > 0:
            drawdown = (peak - equity) / peak * Decimal(100)
            if drawdown > max_drawdown:
                max_drawdown = drawdown
    return max_drawdown


def compute_win_rate_pct(round_trips: list[RoundTrip]) -> Decimal:
    """Percentage of round trips whose exit_price exceeded its entry_price.
    Decimal(0) - not undefined, not fabricated - when there are no round
    trips to grade."""
    if not round_trips:
        return Decimal(0)
    wins = sum(1 for rt in round_trips if rt.exit_price > rt.entry_price)
    return Decimal(wins) / Decimal(len(round_trips)) * Decimal(100)
