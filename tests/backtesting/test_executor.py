"""Unit tests for apps/api/app/backtesting/executor.py.

The headline test here is `test_matches_d025_sma20_crossover_elementwise`:
the generalized evaluator, handed a StrategyDefinition that is the logical
equivalent of D025's hard-coded SMA(20) crossover, must reproduce
apps/api/app/backtesting/strategy.py's signals bar for bar on the same
closes. Not "same length", not "same number of BUYs" - identical at every
index. strategy.py is frozen (D025), so any disagreement is a bug in the
new code, and this test is what would catch it.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.api.app.backtesting import strategy as v1_strategy
from apps.api.app.backtesting.executor import generate_signals
from apps.api.app.backtesting.strategy import Signal
from apps.api.app.marketdata.bar_provider import Bar

_EPOCH = datetime(2026, 1, 1, 21, 0, tzinfo=UTC)


def _bars(closes: list[Decimal]) -> list[Bar]:
    return [
        Bar(
            symbol="TEST",
            bar_interval="1d",
            ts=_EPOCH + timedelta(days=index),
            close=close,
            source="test-fixture",
        )
        for index, close in enumerate(closes)
    ]


def _synthetic_closes() -> list[Decimal]:
    """56 deterministic closes: a flat stretch, a decline, a flat trough, a
    rally, a flat peak, then a decline. Long enough to clear SMA(20)'s
    warmup with room to spare, and shaped so both a BUY and a SELL actually
    occur rather than the whole series degenerating to HOLD (asserted
    below - a series that produced no signals would make the equivalence
    test vacuously true)."""
    values: list[int] = []
    values += [100] * 12
    values += [100 - 2 * (step + 1) for step in range(10)]  # 98 .. 80
    values += [80] * 6
    values += [80 + 4 * (step + 1) for step in range(12)]  # 84 .. 128
    values += [128] * 6
    values += [128 - 6 * (step + 1) for step in range(10)]  # 122 .. 68
    return [Decimal(value) for value in values]


D025_EQUIVALENT_DEFINITION = {
    "indicators": [{"id": "sma_20", "type": "sma", "period": 20}],
    "entry_rule": {"op": "crosses_above", "left": "close", "right": "sma_20"},
    "exit_rule": {"op": "crosses_below", "left": "close", "right": "sma_20"},
    "position_sizing": {"type": "all_in"},
}


def test_d025_definition_is_structurally_valid() -> None:
    # generate_signals' documented precondition is validate_definition() == [];
    # the equivalence test below would be testing an unreachable input if the
    # definition it uses could not actually be saved.
    from apps.api.app.strategies.validation import validate_definition

    assert validate_definition(D025_EQUIVALENT_DEFINITION) == []


def test_matches_d025_sma20_crossover_elementwise() -> None:
    closes = _synthetic_closes()
    assert len(closes) >= 40

    expected = v1_strategy.generate_signals(closes, period=20)
    actual = generate_signals(_bars(closes), D025_EQUIVALENT_DEFINITION)

    # Guard against a vacuous pass: if the series produced nothing but HOLD,
    # "identical" would prove nothing about the crossing logic.
    assert Signal.BUY in expected
    assert Signal.SELL in expected

    assert len(actual) == len(expected)
    mismatches = [
        (index, expected[index], actual[index])
        for index in range(len(expected))
        if expected[index] is not actual[index]
    ]
    assert mismatches == []
    assert actual == expected


@pytest.mark.parametrize("period", [2, 3, 5, 19, 20, 21, 55])
def test_matches_d025_crossover_elementwise_at_every_warmup_boundary(period: int) -> None:
    # The same equivalence at other periods, including period == len-1,
    # len, and len+1. If the executor's warmup boundary were off by one
    # relative to strategy.py's `period`-length HOLD prefix, one of these
    # would disagree at exactly one index.
    closes = _synthetic_closes()
    definition = dict(D025_EQUIVALENT_DEFINITION)
    definition["indicators"] = [{"id": "sma_20", "type": "sma", "period": period}]

    expected = v1_strategy.generate_signals(closes, period=period)
    actual = generate_signals(_bars(closes), definition)
    assert actual == expected


def test_matches_d025_on_a_series_shorter_than_the_warmup() -> None:
    # strategy.py returns all-HOLD for n <= period via an explicit early
    # return; the executor gets there by every crossing evaluating to None.
    # Same answer, arrived at differently - worth pinning.
    closes = _synthetic_closes()[:15]
    expected = v1_strategy.generate_signals(closes, period=20)
    actual = generate_signals(_bars(closes), D025_EQUIVALENT_DEFINITION)
    assert expected == [Signal.HOLD] * 15
    assert actual == expected


def test_bars_shorter_than_warmup_are_all_hold_not_a_crash() -> None:
    closes = [Decimal(100), Decimal(101), Decimal(102)]
    signals = generate_signals(_bars(closes), D025_EQUIVALENT_DEFINITION)
    assert signals == [Signal.HOLD, Signal.HOLD, Signal.HOLD]


def test_empty_bars_gives_empty_signals() -> None:
    assert generate_signals([], D025_EQUIVALENT_DEFINITION) == []


def test_one_signal_per_bar() -> None:
    bars = _bars(_synthetic_closes())
    assert len(generate_signals(bars, D025_EQUIVALENT_DEFINITION)) == len(bars)


# ------------------------------------------------- no indicators at all


def _close_threshold_definition(entry_right: int, exit_right: int) -> dict:
    """A definition with an empty `indicators` list: both rules compare
    `close` against a fixed number, so nothing needs any warmup."""
    return {
        "indicators": [],
        "entry_rule": {"op": "gt", "left": "close", "right": entry_right},
        "exit_rule": {"op": "lt", "left": "close", "right": exit_right},
        "position_sizing": {"type": "all_in"},
    }


def test_no_indicators_rule_that_always_fires() -> None:
    closes = [Decimal(100), Decimal(101), Decimal(102)]
    # Bar.close is constrained > 0, so "close > 0" holds on every bar, and
    # "close < 0" on none.
    definition = _close_threshold_definition(entry_right=0, exit_right=0)
    assert generate_signals(_bars(closes), definition) == [Signal.BUY] * 3


def test_no_indicators_rule_that_never_fires() -> None:
    closes = [Decimal(100), Decimal(101), Decimal(102)]
    definition = _close_threshold_definition(entry_right=1_000, exit_right=0)
    assert generate_signals(_bars(closes), definition) == [Signal.HOLD] * 3


def test_exit_wins_when_both_rules_fire_on_the_same_bar() -> None:
    # Both rules hold on every bar (close > 0 and close < 1000). This
    # function has no notion of current position, so it must surface the
    # exit: a caller already in a trade needs the SELL, and a flat caller
    # simply ignores it - the same way D025's engine ignores a SELL while
    # flat.
    closes = [Decimal(100), Decimal(101)]
    definition = {
        "indicators": [],
        "entry_rule": {"op": "gt", "left": "close", "right": 0},
        "exit_rule": {"op": "lt", "left": "close", "right": 1_000},
        "position_sizing": {"type": "all_in"},
    }
    assert generate_signals(_bars(closes), definition) == [Signal.SELL, Signal.SELL]


# ------------------------------------------------------------- RSI strategies


RSI_DEFINITION = {
    "indicators": [{"id": "rsi_3", "type": "rsi", "period": 3}],
    "entry_rule": {"op": "lt", "left": "rsi_3", "right": 30},
    "exit_rule": {"op": "gt", "left": "rsi_3", "right": 70},
    "position_sizing": {"type": "all_in"},
}


def test_rsi_definition_is_structurally_valid() -> None:
    from apps.api.app.strategies.validation import validate_definition

    assert validate_definition(RSI_DEFINITION) == []


def test_rsi_oversold_overbought_definition() -> None:
    # A pure decline (RSI -> 0, oversold) followed by a pure rally
    # (RSI -> 100, overbought), so both rules get to fire.
    closes = [Decimal(v) for v in (100, 90, 80, 70, 60, 70, 80, 90, 100, 110)]
    signals = generate_signals(_bars(closes), RSI_DEFINITION)

    # RSI(3) needs 4 closes, so indices 0..2 have no value at all -> HOLD,
    # never a fabricated signal.
    assert signals[:3] == [Signal.HOLD] * 3
    assert signals[3] is Signal.BUY  # all losses -> RSI 0 -> below 30
    assert signals[-1] is Signal.SELL  # all gains -> RSI 100 -> above 70
    assert len(signals) == len(closes)


def test_rsi_warmup_is_all_hold_when_no_bar_has_enough_history() -> None:
    closes = [Decimal(100), Decimal(90), Decimal(80)]
    assert generate_signals(_bars(closes), RSI_DEFINITION) == [Signal.HOLD] * 3


# ----------------------------------------------------------- precondition


def test_indicator_type_outside_the_vocabulary_raises_value_error() -> None:
    # Cannot happen for a definition that passed validate_definition. It
    # fails loudly rather than emitting signals from a definition it did not
    # understand.
    #
    # The example used to be `ema`, which Phase 70 added to the vocabulary -
    # so this test started passing for the wrong reason (nothing raised
    # because nothing was unknown any more). It is now a name no one is
    # likely to implement, which is the property this test actually needs:
    # what is asserted is the REFUSAL of an unknown type, not any particular
    # type's absence. Adding a real indicator must never silently disarm it
    # again.
    definition = {
        "indicators": [{"id": "x_10", "type": "not_a_real_indicator", "period": 10}],
        "entry_rule": {"op": "gt", "left": "close", "right": "x_10"},
        "exit_rule": {"op": "lt", "left": "close", "right": "x_10"},
        "position_sizing": {"type": "all_in"},
    }
    with pytest.raises(ValueError, match="unknown indicator type"):
        generate_signals(_bars([Decimal(100)] * 20), definition)
