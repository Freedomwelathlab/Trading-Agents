from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.marketdata.models import MarketSnapshot


def test_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        MarketSnapshot(
            symbol="AAPL", price=Decimal(100), as_of=datetime(2026, 8, 23, 12, 0, 0), source="test"
        )


def test_rejects_non_positive_price():
    with pytest.raises(ValueError, match="greater than 0"):
        MarketSnapshot(
            symbol="AAPL",
            price=Decimal(0),
            as_of=datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC),
            source="test",
        )


def test_accepts_a_well_formed_snapshot():
    snapshot = MarketSnapshot(
        symbol="AAPL",
        price=Decimal(100),
        as_of=datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC),
        source="test",
    )
    assert snapshot.symbol == "AAPL"
