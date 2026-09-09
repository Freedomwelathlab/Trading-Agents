"""Unit tests for apps/api/app/strategies/perturbation.py.

Two properties get asserted everywhere rather than in one dedicated test,
because they are the properties that make the whole module trustworthy and
they are cheap to check at every call site:

  * ONE parameter moved. Every other parameter in a variant is asserted
    identical to the original - it is not enough that the intended one
    changed, since a shallow copy would also pass that check while quietly
    sharing (and mutating) the caller's nested lists.
  * NO binary floats. `_assert_no_floats` walks every returned definition;
    a float appearing anywhere in a perturbed definition would mean some
    arithmetic left Decimal, which is the one thing this module must never
    do.
"""

import copy
from decimal import Decimal

from apps.api.app.strategies.perturbation import (
    Perturbation,
    generate_perturbed_definitions,
)
from apps.api.app.strategies.validation import validate_definition


def _definition(
    indicators: list[dict] | None = None,
    position_sizing: dict | None = None,
) -> dict:
    """A definition that passes `validate_definition` when left alone.

    The rules reference `close` and a fixed number only, so an `indicators`
    list can be swapped in or emptied without dragging a rule into an
    undeclared-id error that has nothing to do with what is being tested.
    """
    return {
        "indicators": [] if indicators is None else indicators,
        "entry_rule": {"op": "crosses_above", "left": "close", "right": 100},
        "exit_rule": {"op": "crosses_below", "left": "close", "right": 90},
        "position_sizing": {"type": "all_in"} if position_sizing is None else position_sizing,
    }


def _sma(period: int, id_: str = "sma_20") -> dict:
    return {"id": id_, "type": "sma", "period": period}


def _assert_no_floats(value: object, where: str = "definition") -> None:
    """Every number reachable in `value` is an `int` or an exact `Decimal`.

    `bool` is checked before `int` because Python makes it a subclass; a
    `True` sitting where a period belongs would otherwise slip through as
    the integer 1.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_no_floats(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_floats(item, f"{where}[{index}]")
    elif isinstance(value, bool):
        raise AssertionError(f"{where} is a boolean: {value!r}")
    elif isinstance(value, float):
        raise AssertionError(f"{where} is a float: {value!r}")
    else:
        assert isinstance(value, str | int | Decimal), f"{where} is {type(value).__name__}"


def _assert_isolated(result: Perturbation, original: dict) -> None:
    """The variant is a deep copy: mutating it cannot reach `original`.

    Checked by identity on the nested containers rather than by equality,
    because a shallow `dict(...)` copy produces a variant that compares
    unequal on the changed leaf while still sharing the very lists that
    hold it.
    """
    assert result.definition is not original
    assert result.definition["indicators"] is not original["indicators"]
    assert result.definition["position_sizing"] is not original["position_sizing"]
    for index, indicator in enumerate(original["indicators"]):
        assert result.definition["indicators"][index] is not indicator


# ------------------------------------------------------------------ periods


def test_single_indicator_produces_one_down_and_one_up_variant() -> None:
    # 20 ± 10% is exactly 18 and 22, so the rounding rule is pinned here
    # without any half-way ambiguity to argue about.
    definition = _definition([_sma(20)])
    snapshot = copy.deepcopy(definition)

    results = generate_perturbed_definitions(definition, magnitude_pct=Decimal("10"))

    assert len(results) == 2
    assert [r.direction for r in results] == ["-", "+"]
    assert [r.parameter_path for r in results] == [
        "indicators[0].period",
        "indicators[0].period",
    ]
    assert [r.perturbed_value for r in results] == [Decimal(18), Decimal(22)]
    assert [r.definition["indicators"][0]["period"] for r in results] == [18, 22]
    assert all(r.original_value == Decimal(20) for r in results)
    assert all(r.clamped is False for r in results)

    for result in results:
        assert validate_definition(result.definition) == []
        _assert_no_floats(result.definition)
        _assert_isolated(result, definition)
    # The caller's original is untouched by any of it.
    assert definition == snapshot


def test_period_is_written_back_as_a_plain_int_not_a_decimal() -> None:
    # validation.py::_is_positive_int requires a real `int`; a Decimal(18)
    # here would make every perturbed definition fail to re-validate.
    results = generate_perturbed_definitions(
        _definition([_sma(20)]), magnitude_pct=Decimal("10")
    )
    for result in results:
        period = result.definition["indicators"][0]["period"]
        assert type(period) is int


def test_period_of_one_skips_the_downward_direction_entirely() -> None:
    # 1 - 50% rounds to 0, which is floored back to 1 - i.e. to the original
    # value. A variant identical to the original would report a 0% change in
    # the backtest and read as evidence of robustness while testing nothing,
    # so it is omitted rather than emitted with clamped=True.
    results = generate_perturbed_definitions(
        _definition([_sma(1, "sma_1")]), magnitude_pct=Decimal("50")
    )

    assert len(results) == 1
    assert results[0].direction == "+"
    # 1 * 1.5 = 1.5, rounded half-to-even -> 2.
    assert results[0].perturbed_value == Decimal(2)
    assert results[0].definition["indicators"][0]["period"] == 2
    assert results[0].clamped is False


def test_period_downward_clamp_that_does_change_the_value_is_kept() -> None:
    # 4 - 90% = 0.4, rounds to 0, floored to 1: a real change from 4, so it
    # is emitted, flagged as clamped so a report knows the nudge was not the
    # nominal 90%.
    results = generate_perturbed_definitions(
        _definition([_sma(4, "sma_4")]), magnitude_pct=Decimal("90")
    )
    down = results[0]

    assert down.direction == "-"
    assert down.perturbed_value == Decimal(1)
    assert down.clamped is True
    assert validate_definition(down.definition) == []


def test_multiple_indicators_each_move_alone() -> None:
    indicators = [_sma(20), {"id": "rsi_14", "type": "rsi", "period": 14}]
    definition = _definition(indicators)

    results = generate_perturbed_definitions(definition, magnitude_pct=Decimal("10"))

    assert len(results) == 4
    assert [r.parameter_path for r in results] == [
        "indicators[0].period",
        "indicators[0].period",
        "indicators[1].period",
        "indicators[1].period",
    ]
    assert [r.direction for r in results] == ["-", "+", "-", "+"]
    # 20 ± 10% -> 18 / 22;  14 ± 10% -> 12.6 -> 13 / 15.4 -> 15.
    assert [r.perturbed_value for r in results] == [
        Decimal(18),
        Decimal(22),
        Decimal(13),
        Decimal(15),
    ]

    # The indicator NOT being perturbed is bit-for-bit the original in every
    # variant - the whole premise of one-factor-at-a-time.
    for result in results[:2]:
        assert result.definition["indicators"][1] == indicators[1]
        assert result.definition["indicators"][1]["period"] == 14
    for result in results[2:]:
        assert result.definition["indicators"][0] == indicators[0]
        assert result.definition["indicators"][0]["period"] == 20

    for result in results:
        assert validate_definition(result.definition) == []
        _assert_no_floats(result.definition)
        _assert_isolated(result, definition)


def test_indicator_ids_and_types_are_never_touched() -> None:
    definition = _definition([_sma(20)])
    results = generate_perturbed_definitions(definition, magnitude_pct=Decimal("25"))
    for result in results:
        indicator = result.definition["indicators"][0]
        assert indicator["id"] == "sma_20"
        assert indicator["type"] == "sma"
        assert set(indicator) == {"id", "type", "period"}


# ------------------------------------------------------------- fixed_fraction


def test_fixed_fraction_moves_both_ways_without_clamping() -> None:
    definition = _definition(
        position_sizing={"type": "fixed_fraction", "fraction": Decimal("0.5")}
    )

    results = generate_perturbed_definitions(definition, magnitude_pct=Decimal("10"))

    assert len(results) == 2
    assert [r.parameter_path for r in results] == [
        "position_sizing.fraction",
        "position_sizing.fraction",
    ]
    assert [r.direction for r in results] == ["-", "+"]
    assert [r.perturbed_value for r in results] == [Decimal("0.45"), Decimal("0.55")]
    assert [r.definition["position_sizing"]["fraction"] for r in results] == [
        Decimal("0.45"),
        Decimal("0.55"),
    ]
    assert all(r.original_value == Decimal("0.5") for r in results)
    assert all(r.clamped is False for r in results)
    for result in results:
        _assert_no_floats(result.definition)
        _assert_isolated(result, definition)


def test_fixed_fraction_upward_nudge_clamps_at_one() -> None:
    # 0.95 * 1.2 = 1.14, which is outside the (0, 1] the closed vocabulary
    # allows. Clamped to 1 and flagged, rather than emitted as a definition
    # that could never validate.
    results = generate_perturbed_definitions(
        _definition(position_sizing={"type": "fixed_fraction", "fraction": Decimal("0.95")}),
        magnitude_pct=Decimal("20"),
    )

    assert len(results) == 2
    down, up = results
    assert down.perturbed_value == Decimal("0.76")
    assert down.clamped is False
    assert up.direction == "+"
    assert up.perturbed_value == Decimal(1)
    assert up.definition["position_sizing"]["fraction"] == Decimal(1)
    assert up.clamped is True


def test_fraction_of_one_skips_upward_and_keeps_downward() -> None:
    # 1 * 1.1 = 1.1 clamps back to 1, i.e. to the original: nothing to test,
    # so the direction is dropped. The downward nudge is unaffected.
    results = generate_perturbed_definitions(
        _definition(position_sizing={"type": "fixed_fraction", "fraction": Decimal(1)}),
        magnitude_pct=Decimal("10"),
    )

    assert len(results) == 1
    assert results[0].direction == "-"
    assert results[0].perturbed_value == Decimal("0.9")
    assert results[0].clamped is False


def test_fraction_downward_nudge_past_zero_clamps_to_the_positive_floor() -> None:
    # A 100% downward nudge is arithmetically 0, which is not a legal
    # fraction. The floor is 0.01, not 0.
    results = generate_perturbed_definitions(
        _definition(position_sizing={"type": "fixed_fraction", "fraction": Decimal("0.5")}),
        magnitude_pct=Decimal("100"),
    )
    down = results[0]

    assert down.direction == "-"
    assert down.perturbed_value == Decimal("0.01")
    assert down.clamped is True


# ------------------------------------------------------------ fixed_notional


def test_fixed_notional_moves_both_ways_without_clamping() -> None:
    definition = _definition(
        position_sizing={"type": "fixed_notional", "amount": Decimal("1000")}
    )

    results = generate_perturbed_definitions(definition, magnitude_pct=Decimal("10"))

    assert len(results) == 2
    assert [r.parameter_path for r in results] == [
        "position_sizing.amount",
        "position_sizing.amount",
    ]
    assert [r.direction for r in results] == ["-", "+"]
    assert [r.perturbed_value for r in results] == [Decimal(900), Decimal(1100)]
    assert [r.definition["position_sizing"]["amount"] for r in results] == [
        Decimal(900),
        Decimal(1100),
    ]
    assert all(r.original_value == Decimal(1000) for r in results)
    assert all(r.clamped is False for r in results)
    for result in results:
        _assert_no_floats(result.definition)
        _assert_isolated(result, definition)


def test_fixed_notional_has_no_upper_bound_to_clamp_against() -> None:
    # Unlike `fraction`, the vocabulary bounds `amount` only from below, so
    # a large upward nudge is emitted unclamped.
    results = generate_perturbed_definitions(
        _definition(position_sizing={"type": "fixed_notional", "amount": Decimal("1000")}),
        magnitude_pct=Decimal("500"),
    )
    up = results[1]

    assert up.perturbed_value == Decimal(6000)
    assert up.clamped is False


def test_fixed_notional_near_zero_clamps_to_the_positive_floor() -> None:
    # 0.02 - 100% = 0, floored to 0.01: a real change, so it is emitted.
    results = generate_perturbed_definitions(
        _definition(position_sizing={"type": "fixed_notional", "amount": Decimal("0.02")}),
        magnitude_pct=Decimal("100"),
    )
    down = results[0]

    assert down.direction == "-"
    assert down.perturbed_value == Decimal("0.01")
    assert down.clamped is True


def test_fixed_notional_at_the_floor_skips_the_downward_direction() -> None:
    # 0.01 - 100% = 0, floored back to 0.01 - the original value, so there
    # is nothing to test in that direction.
    results = generate_perturbed_definitions(
        _definition(position_sizing={"type": "fixed_notional", "amount": Decimal("0.01")}),
        magnitude_pct=Decimal("100"),
    )

    assert len(results) == 1
    assert results[0].direction == "+"
    assert results[0].perturbed_value == Decimal("0.02")


# ---------------------------------------------------------- sizing selection


def test_all_in_sizing_contributes_nothing_but_indicators_still_perturb() -> None:
    definition = _definition([_sma(20)], position_sizing={"type": "all_in"})

    results = generate_perturbed_definitions(definition, magnitude_pct=Decimal("10"))

    assert len(results) == 2
    assert {r.parameter_path for r in results} == {"indicators[0].period"}
    assert not any(r.parameter_path.startswith("position_sizing") for r in results)
    for result in results:
        assert result.definition["position_sizing"] == {"type": "all_in"}


def test_nothing_perturbable_returns_an_empty_list_not_an_error() -> None:
    # A real, valid strategy: `close` versus fixed thresholds, all_in
    # sizing, no indicators. There is simply no numeric parameter to nudge.
    # What that means for a robustness report is the caller's call, so this
    # returns [] rather than raising.
    definition = _definition()
    assert validate_definition(definition) == []

    assert generate_perturbed_definitions(definition, magnitude_pct=Decimal("10")) == []


def test_rules_are_never_perturbed() -> None:
    # The fixed numbers inside entry/exit rules are thresholds, not tunable
    # parameters in this phase's scope - they must come through untouched.
    definition = _definition([_sma(20)])
    results = generate_perturbed_definitions(definition, magnitude_pct=Decimal("10"))
    for result in results:
        assert result.definition["entry_rule"] == definition["entry_rule"]
        assert result.definition["exit_rule"] == definition["exit_rule"]


# ---------------------------------------------------------------- determinism


def test_two_identical_calls_produce_identical_output() -> None:
    definition = _definition(
        [_sma(20), {"id": "rsi_14", "type": "rsi", "period": 14}],
        position_sizing={"type": "fixed_fraction", "fraction": Decimal("0.5")},
    )

    first = generate_perturbed_definitions(definition, magnitude_pct=Decimal("10"))
    second = generate_perturbed_definitions(definition, magnitude_pct=Decimal("10"))

    assert len(first) == len(second) == 6
    for left, right in zip(first, second, strict=True):
        assert left.parameter_path == right.parameter_path
        assert left.direction == right.direction
        assert left.original_value == right.original_value
        assert left.perturbed_value == right.perturbed_value
        assert left.clamped == right.clamped
        assert left.definition == right.definition
        # Equal in value, never the same object - each call deep-copies.
        assert left.definition is not right.definition


def test_indicators_come_before_sizing_in_the_output_order() -> None:
    definition = _definition(
        [_sma(20)],
        position_sizing={"type": "fixed_fraction", "fraction": Decimal("0.5")},
    )

    results = generate_perturbed_definitions(definition, magnitude_pct=Decimal("10"))

    assert [r.parameter_path for r in results] == [
        "indicators[0].period",
        "indicators[0].period",
        "position_sizing.fraction",
        "position_sizing.fraction",
    ]
    assert [r.direction for r in results] == ["-", "+", "-", "+"]


# ------------------------------------------------------------- no float leak


def test_no_returned_value_is_ever_a_binary_float() -> None:
    # All three parameter kinds at once: every number this module WRITES is
    # an int (period) or an exact Decimal (fraction), never a float, and
    # both reported values are Decimals whatever the parameter's own type.
    definition = _definition(
        [_sma(20), {"id": "rsi_14", "type": "rsi", "period": 14}],
        position_sizing={"type": "fixed_fraction", "fraction": Decimal("0.25")},
    )

    results = generate_perturbed_definitions(definition, magnitude_pct=Decimal("10"))

    assert len(results) == 6
    for result in results:
        _assert_no_floats(result.definition)
        assert isinstance(result.original_value, Decimal)
        assert isinstance(result.perturbed_value, Decimal)
        assert not isinstance(result.perturbed_value, float)


def test_a_float_input_is_converted_exactly_and_only_where_it_is_perturbed() -> None:
    # `fraction` arrives from JSONB as a binary float in this codebase (see
    # engine_v2.py's Decimal(str(...)) conversion). Two separate obligations:
    #
    #   * the PERTURBED value is computed through str, so it is exactly
    #     0.25*0.9 and 0.25*1.1 - not 0.225000000000000005... from binary
    #     float arithmetic;
    #   * in a variant perturbing a PERIOD, the fraction is not this
    #     variant's moved parameter, so it must come through bit-for-bit
    #     untouched - converting it to Decimal there would silently move a
    #     second parameter and break one-factor-at-a-time.
    definition = _definition(
        [_sma(20)], position_sizing={"type": "fixed_fraction", "fraction": 0.25}
    )

    results = generate_perturbed_definitions(definition, magnitude_pct=Decimal("10"))

    period_variants = [r for r in results if r.parameter_path == "indicators[0].period"]
    fraction_variants = [
        r for r in results if r.parameter_path == "position_sizing.fraction"
    ]

    for result in period_variants:
        fraction = result.definition["position_sizing"]["fraction"]
        assert fraction == 0.25
        assert type(fraction) is float

    assert [r.original_value for r in fraction_variants] == [Decimal("0.25")] * 2
    assert [r.perturbed_value for r in fraction_variants] == [
        Decimal("0.225"),
        Decimal("0.275"),
    ]
    for result in fraction_variants:
        assert type(result.definition["position_sizing"]["fraction"]) is Decimal


def test_perturbation_is_frozen() -> None:
    # A consumer must not be able to edit a variant's label out from under
    # the record of what was actually changed.
    result = generate_perturbed_definitions(
        _definition([_sma(20)]), magnitude_pct=Decimal("10")
    )[0]
    try:
        result.parameter_path = "tampered"  # type: ignore[misc]
    except Exception as exc:  # dataclasses.FrozenInstanceError
        assert type(exc).__name__ == "FrozenInstanceError"
    else:  # pragma: no cover - only reached if the dataclass stops being frozen
        raise AssertionError("Perturbation should be immutable")
