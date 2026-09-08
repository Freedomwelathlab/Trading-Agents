"""Unit tests for apps/api/app/strategies/expressions.py.

Expected indicator values are never hard-coded here as decimal literals -
they are compared against marketdata/indicators.py's `sma`/`rsi` called
directly on the equivalent window. That is the point of the module: it must
reuse those functions rather than recompute the math a second way, so a
test asserting a literal would happily pass against a reimplementation that
had quietly drifted. The boundaries (where `None` stops and real values
begin) ARE asserted explicitly, because those are this module's own
behavior rather than the indicators' - see the comment on each.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.indicators import rsi, sma
from apps.api.app.strategies.expressions import (
    compute_indicator_series,
    evaluate_rule,
    resolve_operand,
)

_EPOCH = datetime(2026, 1, 1, 21, 0, tzinfo=UTC)


def _bar(day: int, close: str) -> Bar:
    return Bar(
        symbol="TEST",
        bar_interval="1d",
        ts=_EPOCH + timedelta(days=day),
        close=Decimal(close),
        source="test-fixture",
    )


def _bars(closes: list[str]) -> list[Bar]:
    return [_bar(i, close) for i, close in enumerate(closes)]


def _closes(bars: list[Bar]) -> list[Decimal]:
    return [bar.close for bar in bars]


SERIES = ["10", "12", "11", "15", "14", "18", "17", "20", "19", "25", "24", "30"]


# ---------------------------------------------------------------- indicators


def test_sma_series_has_one_entry_per_bar() -> None:
    bars = _bars(SERIES)
    series = compute_indicator_series(bars, {"id": "sma_3", "type": "sma", "period": 3})
    assert len(series) == len(bars)


def test_sma_series_matches_indicators_sma_at_hand_picked_indices() -> None:
    bars = _bars(SERIES)
    closes = _closes(bars)
    series = compute_indicator_series(bars, {"id": "sma_3", "type": "sma", "period": 3})
    for index in (2, 3, 7, len(bars) - 1):
        assert series[index] == sma(closes[: index + 1], 3)


def test_sma_series_none_boundary_is_exactly_period_minus_one() -> None:
    # SMA(period) needs `period` closes, so bars[0..period-1] is the first
    # window that has enough: index period-2 is still None, index period-1 is
    # the first defined value. Asserted for period=4 so the off-by-one has
    # somewhere to hide if it exists.
    period = 4
    bars = _bars(SERIES)
    series = compute_indicator_series(bars, {"id": "sma_4", "type": "sma", "period": period})
    assert series[: period - 1] == [None] * (period - 1)
    assert series[period - 2] is None
    assert series[period - 1] == sma(_closes(bars)[:period], period)
    assert all(value is not None for value in series[period - 1 :])


def test_rsi_series_matches_indicators_rsi_at_hand_picked_indices() -> None:
    bars = _bars(SERIES)
    closes = _closes(bars)
    series = compute_indicator_series(bars, {"id": "rsi_3", "type": "rsi", "period": 3})
    for index in (3, 5, len(bars) - 1):
        assert series[index] == rsi(closes[: index + 1], 3)


def test_rsi_series_none_boundary_is_exactly_period() -> None:
    # RSI(period) needs period+1 closes (it measures `period` *changes*), so
    # its first defined index is `period`, one later than SMA's - the
    # analogous off-by-one check, deliberately a different number.
    period = 3
    bars = _bars(SERIES)
    series = compute_indicator_series(bars, {"id": "rsi_3", "type": "rsi", "period": period})
    assert series[:period] == [None] * period
    assert series[period - 1] is None
    assert series[period] == rsi(_closes(bars)[: period + 1], period)
    assert all(value is not None for value in series[period:])


def test_series_is_all_none_when_no_bar_has_enough_history() -> None:
    bars = _bars(SERIES[:3])
    series = compute_indicator_series(bars, {"id": "sma_20", "type": "sma", "period": 20})
    assert series == [None, None, None]


def test_empty_bars_gives_empty_series() -> None:
    assert compute_indicator_series([], {"id": "sma_3", "type": "sma", "period": 3}) == []


def test_unknown_indicator_type_raises_value_error() -> None:
    # Cannot happen for a definition that passed validate_definition; it
    # fails loudly rather than returning an all-None series that would be
    # indistinguishable from a real data gap.
    with pytest.raises(ValueError, match="unknown indicator type"):
        compute_indicator_series(_bars(SERIES), {"id": "x", "type": "ema", "period": 3})


# ------------------------------------------------------------------ operands


def test_resolve_operand_number_uses_decimal_str_not_float() -> None:
    bar = _bar(0, "100")
    assert resolve_operand(30, bar=bar, indicator_series={}, index=0) == Decimal("30")
    # Decimal(0.1) would be 0.1000000000000000055511151231257827...; the
    # threshold the user wrote is 0.1.
    assert resolve_operand(0.1, bar=bar, indicator_series={}, index=0) == Decimal("0.1")


def test_resolve_operand_close_is_the_passed_bars_close() -> None:
    bar = _bar(0, "123.45")
    assert resolve_operand("close", bar=bar, indicator_series={}, index=7) == Decimal("123.45")


def test_resolve_operand_indicator_id_with_a_defined_value() -> None:
    bar = _bar(0, "100")
    series = {"sma_3": [None, None, Decimal("11"), Decimal("12")]}
    assert resolve_operand("sma_3", bar=bar, indicator_series=series, index=2) == Decimal("11")


def test_resolve_operand_indicator_id_with_no_value_yet_propagates_none() -> None:
    bar = _bar(0, "100")
    series: dict[str, list[Decimal | None]] = {"sma_3": [None, None, Decimal("11")]}
    assert resolve_operand("sma_3", bar=bar, indicator_series=series, index=1) is None


# --------------------------------------------------------------------- rules


def _indicator_series(bars: list[Bar], **specs: int) -> dict[str, list[Decimal | None]]:
    """`_indicator_series(bars, sma_3=3)` -> {"sma_3": SMA(3) series}."""
    return {
        name: compute_indicator_series(
            bars, {"id": name, "type": name.split("_")[0], "period": period}
        )
        for name, period in specs.items()
    }


def test_gt_true_and_false() -> None:
    bars = _bars(["10", "20", "30"])
    rule = {"op": "gt", "left": "close", "right": 15}
    assert evaluate_rule(rule, bars=bars, indicator_series={}, index=0) is False
    assert evaluate_rule(rule, bars=bars, indicator_series={}, index=1) is True


def test_gte_boundary_is_inclusive_where_gt_is_not() -> None:
    bars = _bars(["15"])
    assert evaluate_rule(
        {"op": "gt", "left": "close", "right": 15}, bars=bars, indicator_series={}, index=0
    ) is False
    assert evaluate_rule(
        {"op": "gte", "left": "close", "right": 15}, bars=bars, indicator_series={}, index=0
    ) is True


def test_lt_and_lte() -> None:
    bars = _bars(["15"])
    assert evaluate_rule(
        {"op": "lt", "left": "close", "right": 15}, bars=bars, indicator_series={}, index=0
    ) is False
    assert evaluate_rule(
        {"op": "lte", "left": "close", "right": 15}, bars=bars, indicator_series={}, index=0
    ) is True
    assert evaluate_rule(
        {"op": "lt", "left": "close", "right": 16}, bars=bars, indicator_series={}, index=0
    ) is True


def test_instantaneous_operators_propagate_none_from_an_undefined_indicator() -> None:
    bars = _bars(SERIES)
    series = _indicator_series(bars, sma_4=4)
    # index 2 is inside SMA(4)'s warmup -> the whole rule has no answer, and
    # must not degrade to False (which would read as "the rule was checked
    # and did not hold").
    for op in ("gt", "gte", "lt", "lte"):
        rule = {"op": op, "left": "close", "right": "sma_4"}
        assert evaluate_rule(rule, bars=bars, indicator_series=series, index=2) is None
        assert evaluate_rule(rule, bars=bars, indicator_series=series, index=3) is not None


def test_rule_comparing_an_indicator_against_a_fixed_numeric_literal() -> None:
    # The classic "RSI(3) below 30" oversold rule - indicator vs number,
    # neither side being `close`.
    bars = _bars(["50", "40", "30", "20", "10"])
    series = _indicator_series(bars, rsi_3=3)
    assert series["rsi_3"][3] == Decimal(0)  # a pure downtrend: no gains at all
    assert evaluate_rule(
        {"op": "lt", "left": "rsi_3", "right": 30},
        bars=bars,
        indicator_series=series,
        index=3,
    ) is True
    assert evaluate_rule(
        {"op": "gt", "left": "rsi_3", "right": 30},
        bars=bars,
        indicator_series=series,
        index=3,
    ) is False


def test_rule_comparing_two_indicators_against_each_other() -> None:
    # A fast/slow moving-average rule: neither operand is `close` or a
    # literal, so both sides go through the indicator-series lookup.
    bars = _bars(["10", "10", "10", "10", "10", "20", "30", "40", "50", "60"])
    series = _indicator_series(bars, sma_2=2, sma_5=5)
    fast_over_slow = {"op": "gt", "left": "sma_2", "right": "sma_5"}
    # Flat stretch: fast == slow, so "greater than" does not hold.
    assert evaluate_rule(fast_over_slow, bars=bars, indicator_series=series, index=4) is False
    # After the ramp starts, the 2-bar average leads the 5-bar one.
    assert evaluate_rule(fast_over_slow, bars=bars, indicator_series=series, index=8) is True
    # sma_5 is undefined before index 4 even though sma_2 is defined at 1.
    assert evaluate_rule(fast_over_slow, bars=bars, indicator_series=series, index=3) is None


def test_crosses_above_is_true_only_on_the_bar_the_relationship_changed() -> None:
    # closes: 10, 10, 10, 30 with SMA(2).
    #   sma_2: [None, 10, 10, 20]
    #   index 2: prev close 10 <= prev sma 10, curr close 10 !> curr sma 10 -> False
    #   index 3: prev close 10 <= prev sma 10, curr close 30 >  curr sma 20 -> True
    bars = _bars(["10", "10", "10", "30"])
    series = _indicator_series(bars, sma_2=2)
    rule = {"op": "crosses_above", "left": "close", "right": "sma_2"}
    assert evaluate_rule(rule, bars=bars, indicator_series=series, index=2) is False
    assert evaluate_rule(rule, bars=bars, indicator_series=series, index=3) is True


def test_crosses_above_is_not_sugar_for_gt() -> None:
    # A bar where close is comfortably above the average but was ALSO above
    # it on the previous bar: `gt` holds, `crosses_above` must not.
    bars = _bars(["10", "10", "30", "40"])
    series = _indicator_series(bars, sma_2=2)
    assert evaluate_rule(
        {"op": "gt", "left": "close", "right": "sma_2"},
        bars=bars,
        indicator_series=series,
        index=3,
    ) is True
    assert evaluate_rule(
        {"op": "crosses_above", "left": "close", "right": "sma_2"},
        bars=bars,
        indicator_series=series,
        index=3,
    ) is False


def test_crosses_below_true_and_false() -> None:
    # closes: 30, 30, 30, 10 with SMA(2).
    #   sma_2: [None, 30, 30, 20]
    #   index 2: prev close 30 >= prev sma 30, curr close 30 !< curr sma 30 -> False
    #   index 3: prev close 30 >= prev sma 30, curr close 10 <  curr sma 20 -> True
    bars = _bars(["30", "30", "30", "10"])
    series = _indicator_series(bars, sma_2=2)
    rule = {"op": "crosses_below", "left": "close", "right": "sma_2"}
    assert evaluate_rule(rule, bars=bars, indicator_series=series, index=2) is False
    assert evaluate_rule(rule, bars=bars, indicator_series=series, index=3) is True


def test_crossing_operator_on_index_zero_is_none_not_false() -> None:
    # There is no bar before the first bar of the series handed in, so
    # nothing can be said to have crossed on it. This is exactly why callers
    # must supply warmup bars ahead of the window they want signals for.
    bars = _bars(["10", "20", "30"])
    for op in ("crosses_above", "crosses_below"):
        rule = {"op": op, "left": "close", "right": 15}
        assert evaluate_rule(rule, bars=bars, indicator_series={}, index=0) is None
        assert evaluate_rule(rule, bars=bars, indicator_series={}, index=1) is not None


def test_crossing_operator_is_none_when_the_previous_bars_indicator_is_undefined() -> None:
    # Both bars must resolve, not just the current one: at index 3 with
    # SMA(4), sma_4[2] is still None even though sma_4[3] is defined.
    bars = _bars(SERIES)
    series = _indicator_series(bars, sma_4=4)
    rule = {"op": "crosses_above", "left": "close", "right": "sma_4"}
    assert series["sma_4"][3] is not None
    assert series["sma_4"][2] is None
    assert evaluate_rule(rule, bars=bars, indicator_series=series, index=3) is None
    assert evaluate_rule(rule, bars=bars, indicator_series=series, index=4) is not None


def test_unknown_operator_raises_value_error() -> None:
    bars = _bars(SERIES)
    with pytest.raises(ValueError, match="unknown rule operator"):
        evaluate_rule(
            {"op": "approximately", "left": "close", "right": 10},
            bars=bars,
            indicator_series={},
            index=1,
        )
