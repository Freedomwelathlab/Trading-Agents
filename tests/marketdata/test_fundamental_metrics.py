"""Unit tests for the pure deterministic fundamental metrics (D059) - no
LLM, no I/O. Mirrors tests/marketdata/test_indicators.py's shape.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.marketdata.fundamental_metrics import earnings_yield_pct, latest_point


def test_earnings_yield_is_the_exact_reciprocal_of_pe():
    assert earnings_yield_pct(Decimal("25")) == Decimal("4")
    assert earnings_yield_pct(Decimal("20")) == Decimal("5")


def test_earnings_yield_keeps_decimal_precision_not_float_error():
    result = earnings_yield_pct(Decimal("3"))
    assert isinstance(result, Decimal)
    # Exact Decimal division, not a binary float artefact.
    assert result == Decimal(100) / Decimal(3)


@pytest.mark.parametrize("pe", [Decimal("0"), Decimal("-5")])
def test_earnings_yield_refuses_a_non_positive_pe(pe):
    with pytest.raises(ValueError, match="non-positive"):
        earnings_yield_pct(pe)


def test_latest_point_picks_by_timestamp_not_list_position():
    points = [
        (datetime(2026, 8, 1, tzinfo=UTC), Decimal("1")),
        (datetime(2026, 8, 20, tzinfo=UTC), Decimal("2")),
        (datetime(2026, 8, 10, tzinfo=UTC), Decimal("3")),
    ]
    assert latest_point(points) == (datetime(2026, 8, 20, tzinfo=UTC), Decimal("2"))


def test_latest_point_of_an_empty_series_is_none_not_a_placeholder():
    assert latest_point([]) is None
