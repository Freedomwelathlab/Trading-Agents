"""Unit tests for the Phase 61 signal engine - pure functions over hand-built
bar series, no database, no HTTP, nothing patched.

Every close sequence here is chosen so the expected verdict can be checked by
hand from the numbers in the test itself: SMA(3) over four flat closes is
exactly the flat close, so a fifth close either crosses it or does not, with
no long decimals to squint at. That is the same reasoning
tests/api/test_strategy_backtests.py gives for its alternating 100/105 series.

The DB-backed half of this phase - permissions, ownership, persistence,
listing - is tests/api/test_signals.py's.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.api.app.backtesting.executor import generate_signals
from apps.api.app.backtesting.strategy import Signal
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.signals.engine import evaluate_current_signal
from apps.api.app.strategies.expressions import compute_indicator_series
from apps.api.app.strategies.validation import validate_definition

# SMA(3) crossover - the same shape as Phase 54's SMA(20) example, shrunk so a
# four-bar warmup is enough and every average is a round number.
SMA_CROSSOVER = {
    "indicators": [{"id": "sma_3", "type": "sma", "period": 3}],
    "entry_rule": {"op": "crosses_above", "left": "close", "right": "sma_3"},
    "exit_rule": {"op": "crosses_below", "left": "close", "right": "sma_3"},
    "position_sizing": {"type": "all_in"},
}

# The same indicator read as an instantaneous threshold instead of a crossing.
# This is what makes the warmup-boundary test exact: `gt` needs the indicator
# only at the latest bar, so SMA(3) is evaluable on the third bar and on no
# earlier one.
SMA_THRESHOLD = {
    "indicators": [{"id": "sma_3", "type": "sma", "period": 3}],
    "entry_rule": {"op": "gt", "left": "close", "right": "sma_3"},
    "exit_rule": {"op": "lt", "left": "close", "right": "sma_3"},
    "position_sizing": {"type": "all_in"},
}

# RSI(2) against fixed thresholds - the second indicator type in the closed
# vocabulary, and the one whose warmup boundary differs (period + 1 closes).
RSI_THRESHOLD = {
    "indicators": [{"id": "rsi_2", "type": "rsi", "period": 2}],
    "entry_rule": {"op": "lt", "left": "rsi_2", "right": 30},
    "exit_rule": {"op": "gt", "left": "rsi_2", "right": 70},
    "position_sizing": {"type": "all_in"},
}


def _bars(closes: list[str]) -> list[Bar]:
    """Oldest-first daily bars on consecutive dates from 2026-06-01, with the
    given closes. Only `close` matters to the evaluator, but every field is a
    real value - nothing here is padded or interpolated."""
    return [
        Bar(
            symbol="SIGQA.US",
            bar_interval="1d",
            ts=datetime(2026, 6, 1 + index, 21, 0, tzinfo=UTC),
            close=Decimal(close),
            source="test-signal-engine",
        )
        for index, close in enumerate(closes)
    ]


def test_the_three_fixture_definitions_are_really_valid() -> None:
    """`evaluate_current_signal`'s documented precondition is a definition off
    a VALIDATED version. If these fixtures drifted out of the closed
    vocabulary, every other test here would be exercising a path the real
    routes can never reach."""
    for definition in (SMA_CROSSOVER, SMA_THRESHOLD, RSI_THRESHOLD):
        assert validate_definition(definition) == []


def test_a_fresh_cross_up_on_the_latest_bar_is_buy_and_says_the_numbers() -> None:
    """Four flat closes at 100 put SMA(3) at exactly 100, so the fifth close
    at 112 is above both it and the new average (312/3 = 104) - a cross that
    happened on this bar, not one that was already in place."""
    bars = _bars(["100", "100", "100", "100", "112"])

    current = evaluate_current_signal(bars, SMA_CROSSOVER)

    assert current.signal is Signal.BUY
    assert current.entry_rule_held is True
    assert current.exit_rule_held is False
    assert current.insufficient_data is False
    assert current.as_of == bars[-1].ts
    assert current.latest_close == Decimal(112)
    assert current.indicator_values == {"sma_3": Decimal(104)}
    # The explanation names both sides of the comparison, the bar it was made
    # on, and the verdict - the "never a black-box BUY" contract.
    assert current.explanation == (
        "close (112) crossed above sma_3 (104) on the latest bar (2026-06-05); "
        "entry rule holds -> BUY"
    )


def test_already_above_without_crossing_is_hold_and_says_why_nothing_fired() -> None:
    """A steadily rising series is above its own SMA on every bar, so the
    crossing rule fires on none of them. The explanation has to distinguish
    that from being below - which is exactly what "did not cross above" says
    and "is not above" would not."""
    bars = _bars(["100", "110", "120", "130", "140"])

    current = evaluate_current_signal(bars, SMA_CROSSOVER)

    assert current.signal is Signal.HOLD
    assert current.entry_rule_held is False
    assert current.exit_rule_held is False
    assert current.insufficient_data is False
    assert current.explanation == (
        "on the latest bar (2026-06-05) close (140) did not cross above sma_3 (130), "
        "and close (140) did not cross below sma_3 (130); neither rule fired -> HOLD"
    )


def test_a_cross_down_on_the_latest_bar_is_sell() -> None:
    bars = _bars(["100", "100", "100", "100", "88"])

    current = evaluate_current_signal(bars, SMA_CROSSOVER)

    assert current.signal is Signal.SELL
    assert current.exit_rule_held is True
    assert current.entry_rule_held is False
    assert current.insufficient_data is False
    assert current.indicator_values == {"sma_3": Decimal(96)}
    assert current.explanation == (
        "exit rule holds: close (88) crossed below sma_3 (96) on the latest bar "
        "(2026-06-05) -> SELL"
    )


def test_exactly_at_the_warmup_boundary_evaluates_and_one_bar_short_does_not() -> None:
    """SMA(3) read as a threshold is defined on the third bar and on no
    earlier one, so three bars is the boundary. One short is not "no signal" -
    it is `insufficient_data`, with an explanation naming the real bar count.
    """
    at_boundary = evaluate_current_signal(_bars(["100", "100", "112"]), SMA_THRESHOLD)
    assert at_boundary.insufficient_data is False
    assert at_boundary.signal is Signal.BUY
    assert at_boundary.entry_rule_held is True
    assert at_boundary.indicator_values == {"sma_3": Decimal(104)}

    one_short = evaluate_current_signal(_bars(["100", "100"]), SMA_THRESHOLD)
    assert one_short.insufficient_data is True
    assert one_short.signal is Signal.HOLD
    assert one_short.entry_rule_held is None
    assert one_short.exit_rule_held is None
    # The value is None, never 0 and never the last known number.
    assert one_short.indicator_values == {"sma_3": None}
    assert one_short.explanation == (
        "sma_3 (period 3) has no value at the latest bar (2026-06-02): it needs more "
        "than the 2 bars ingested for this symbol -> HOLD (insufficient data)"
    )


def test_rsi_thresholds_produce_buy_sell_and_hold_from_the_same_definition() -> None:
    """RSI(2) needs three closes. Two straight losses pin it at 0 (below the
    entry threshold), two straight gains at 100 (above the exit threshold),
    and an equal gain-then-loss at exactly 50 (between the two)."""
    oversold = evaluate_current_signal(_bars(["100", "95", "90"]), RSI_THRESHOLD)
    assert oversold.signal is Signal.BUY
    assert oversold.indicator_values == {"rsi_2": Decimal(0)}
    assert oversold.explanation == (
        "rsi_2 (0) is below 30 on the latest bar (2026-06-03); entry rule holds -> BUY"
    )

    overbought = evaluate_current_signal(_bars(["100", "105", "110"]), RSI_THRESHOLD)
    assert overbought.signal is Signal.SELL
    assert overbought.indicator_values == {"rsi_2": Decimal(100)}
    assert overbought.explanation == (
        "exit rule holds: rsi_2 (100) is above 70 on the latest bar (2026-06-03) -> SELL"
    )

    neutral = evaluate_current_signal(_bars(["100", "105", "100"]), RSI_THRESHOLD)
    assert neutral.signal is Signal.HOLD
    assert neutral.insufficient_data is False
    assert neutral.indicator_values == {"rsi_2": Decimal(50)}
    assert "rsi_2 (50) is not below 30" in neutral.explanation
    assert "rsi_2 (50) is not above 70" in neutral.explanation


def test_rsi_one_bar_short_of_its_own_boundary_is_insufficient_data() -> None:
    """RSI(period) needs `period + 1` closes, one more than SMA(period) - and
    the engine gets that boundary from the indicator function itself rather
    than restating it, which is what this pins."""
    current = evaluate_current_signal(_bars(["100", "95"]), RSI_THRESHOLD)

    assert current.insufficient_data is True
    assert current.signal is Signal.HOLD
    assert current.indicator_values == {"rsi_2": None}
    assert "rsi_2 (period 2) has no value at the latest bar" in current.explanation
    assert "the 2 bars ingested" in current.explanation


def test_no_bars_at_all_is_the_documented_empty_shape() -> None:
    current = evaluate_current_signal([], SMA_CROSSOVER)

    assert current.signal is Signal.HOLD
    assert current.as_of is None
    assert current.latest_close is None
    assert current.entry_rule_held is None
    assert current.exit_rule_held is None
    assert current.insufficient_data is True
    assert current.indicator_values == {}
    assert current.explanation == (
        "no bars are ingested for this symbol -> HOLD (insufficient data)"
    )


def test_a_single_bar_cannot_support_a_crossing_and_says_so() -> None:
    """One bar is enough for SMA(1) to be defined but not for any crossing to
    be measured - a different reason for the same insufficiency, and the
    explanation says which."""
    definition = {
        "indicators": [{"id": "sma_1", "type": "sma", "period": 1}],
        "entry_rule": {"op": "crosses_above", "left": "close", "right": "sma_1"},
        "exit_rule": {"op": "crosses_below", "left": "close", "right": "sma_1"},
        "position_sizing": {"type": "all_in"},
    }
    assert validate_definition(definition) == []

    current = evaluate_current_signal(_bars(["100"]), definition)

    assert current.insufficient_data is True
    assert current.signal is Signal.HOLD
    # The indicator DOES have a value - the missing thing is the previous bar.
    assert current.indicator_values == {"sma_1": Decimal(100)}
    assert current.explanation == (
        "only 1 bar is ingested for this symbol, so there is no previous bar to "
        "measure a crossing against -> HOLD (insufficient data)"
    )


@pytest.mark.parametrize(
    "definition",
    [SMA_CROSSOVER, SMA_THRESHOLD, RSI_THRESHOLD],
    ids=["sma_crossover", "sma_threshold", "rsi_threshold"],
)
@pytest.mark.parametrize(
    "closes",
    [
        ["100", "100", "100", "100", "112"],
        ["100", "100", "100", "100", "88"],
        ["100", "110", "120", "130", "140"],
        ["140", "130", "120", "110", "100"],
        ["100", "105", "100", "105", "100", "105"],
        ["100", "100", "100"],
        ["100", "100"],
        ["100"],
    ],
    ids=[
        "cross_up",
        "cross_down",
        "rising",
        "falling",
        "alternating",
        "flat_three",
        "two_bars",
        "one_bar",
    ],
)
def test_the_headline_signal_never_disagrees_with_the_backtest_executor(
    definition: dict, closes: list[str]
) -> None:
    """The property that makes a current signal trustworthy: it is the LAST
    element of the exact signal series a backtest over these same bars would
    produce, across every definition and every series shape. If this ever
    fails, the engine has grown a second opinion - which is the one thing it
    must never do."""
    bars = _bars(closes)

    assert evaluate_current_signal(bars, definition).signal == generate_signals(
        bars, definition
    )[-1]


@pytest.mark.parametrize(
    "definition",
    [SMA_CROSSOVER, RSI_THRESHOLD],
    ids=["sma_crossover", "rsi_threshold"],
)
def test_indicator_values_are_the_series_last_entries_not_a_second_computation(
    definition: dict,
) -> None:
    """Same discipline as the signal itself: the reported indicator values are
    `compute_indicator_series(...)[-1]`, not numbers this module worked out
    its own way."""
    bars = _bars(["100", "104", "99", "107", "103", "111"])

    current = evaluate_current_signal(bars, definition)

    assert current.indicator_values == {
        indicator["id"]: compute_indicator_series(bars, indicator)[-1]
        for indicator in definition["indicators"]
    }
