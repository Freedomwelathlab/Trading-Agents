"""Deterministic fundamental metrics - pure functions, no LLM, no I/O, no
DB. Exactly the role apps/api/app/marketdata/indicators.py plays for
TechnicalAnalyst (D021), now for FundamentalAnalyst (D059): the LLM may
narrate a number these functions produced, and may never produce one
itself (docs/TOKEN_POLICY.md's mandatory-deterministic rule).

Kept deliberately small. The Longbridge vendor already returns PE/PB/PS/
dividend yield as real reported values, so there is nothing to
recalculate - only genuinely derived quantities belong here, and only
where the derivation is exact arithmetic rather than a modelling choice.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal


def earnings_yield_pct(pe: Decimal) -> Decimal:
    """Earnings yield as a percentage: the exact reciprocal of the P/E
    ratio, 100 / PE. Not an estimate and not a model - it is the same
    number the PE already states, expressed the other way up, which is
    why computing it here is safe where asking an LLM for it would not
    be.

    A non-positive PE has no meaningful earnings yield (a loss-making
    company); raising is the honest answer, since any returned number
    would be a made-up one.
    """
    if pe <= 0:
        raise ValueError("earnings yield is undefined for a non-positive P/E")
    return Decimal(100) / pe


def latest_point(points: Sequence[tuple[datetime, Decimal]]) -> tuple[datetime, Decimal] | None:
    """The most recent (timestamp, value) pair, chosen by timestamp -
    never by the vendor's list order, which is not contractually sorted.
    Returns None for an empty series rather than a placeholder, so a
    caller cannot mistake "no data" for a value.
    """
    if not points:
        return None
    return max(points, key=lambda point: point[0])
