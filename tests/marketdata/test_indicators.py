from decimal import Decimal

import pytest

from apps.api.app.marketdata.indicators import InsufficientDataError, rsi, sma


def test_sma_averages_the_most_recent_window():
    closes = [Decimal(v) for v in [1, 2, 3, 4, 5]]
    assert sma(closes, 5) == Decimal(3)
    assert sma(closes, 3) == Decimal(4)


def test_sma_raises_on_insufficient_data_rather_than_padding():
    closes = [Decimal(v) for v in [1, 2]]
    with pytest.raises(InsufficientDataError):
        sma(closes, 5)


def test_sma_rejects_a_non_positive_period():
    with pytest.raises(ValueError):
        sma([Decimal(1)], 0)


def test_rsi_is_100_for_a_strictly_increasing_series():
    closes = [Decimal(v) for v in range(1, 16)]  # 15 closes, period 14
    assert rsi(closes, 14) == Decimal(100)


def test_rsi_is_0_for_a_strictly_decreasing_series():
    closes = [Decimal(v) for v in range(15, 0, -1)]
    assert rsi(closes, 14) == Decimal(0)


def test_rsi_is_50_for_a_flat_series():
    closes = [Decimal(10)] * 15
    assert rsi(closes, 14) == Decimal(50)


def test_rsi_raises_on_insufficient_data():
    closes = [Decimal(v) for v in [1, 2, 3]]
    with pytest.raises(InsufficientDataError):
        rsi(closes, 14)


def test_rsi_rejects_a_non_positive_period():
    with pytest.raises(ValueError):
        rsi([Decimal(1), Decimal(2)], 0)
