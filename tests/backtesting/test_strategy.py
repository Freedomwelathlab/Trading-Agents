"""Unit tests for the SMA-crossover signal logic in
apps/api/app/backtesting/strategy.py. Signals are hand-derived from the
same SMA(period) definition marketdata/indicators.py already tests
independently - see the comment above each expected value."""

from decimal import Decimal

from apps.api.app.backtesting.strategy import Signal, generate_signals


def _d(*values: int) -> list[Decimal]:
    return [Decimal(v) for v in values]


def test_too_short_a_series_is_all_hold() -> None:
    closes = _d(10, 11, 12)  # period=3, len(closes)==period -> no signal possible
    assert generate_signals(closes, period=3) == [Signal.HOLD, Signal.HOLD, Signal.HOLD]


def test_warmup_period_is_always_hold() -> None:
    closes = _d(10, 10, 10, 5, 20, 21, 5)
    signals = generate_signals(closes, period=3)
    assert signals[:3] == [Signal.HOLD, Signal.HOLD, Signal.HOLD]


def test_hand_computed_crossover_sequence() -> None:
    # closes = [10, 10, 10, 5, 20, 21, 5], period=3
    # SMA(3) at each index i>=2: avg(closes[i-2:i+1])
    #   i=2: avg(10,10,10) = 10
    #   i=3: avg(10,10,5)  = 8.333...
    #   i=4: avg(10,5,20)  = 11.666...
    #   i=5: avg(5,20,21)  = 15.333...
    #   i=6: avg(20,21,5)  = 15.333...
    #
    # i=3: prev_close=10 <= prev_sma(10)=10 and curr_close=5 < curr_sma(8.33) -> SELL
    # i=4: prev_close=5 <= prev_sma(8.33) and curr_close=20 > curr_sma(11.67) -> BUY
    # i=5: prev_close=20 >= prev_sma(11.67) but curr_close=21 !< curr_sma(15.33) -> HOLD
    # i=6: prev_close=21 >= prev_sma(15.33) and curr_close=5 < curr_sma(15.33) -> SELL
    closes = _d(10, 10, 10, 5, 20, 21, 5)
    signals = generate_signals(closes, period=3)
    assert signals == [
        Signal.HOLD,
        Signal.HOLD,
        Signal.HOLD,
        Signal.SELL,
        Signal.BUY,
        Signal.HOLD,
        Signal.SELL,
    ]


def test_strictly_flat_series_never_signals() -> None:
    closes = _d(*([100] * 10))
    signals = generate_signals(closes, period=3)
    assert all(s is Signal.HOLD for s in signals)


def test_output_length_matches_input_length() -> None:
    closes = _d(*range(1, 31))
    signals = generate_signals(closes, period=20)
    assert len(signals) == len(closes)
