"""Pure unit tests for the structural strategy-definition validator
(Phase 54). No database, no app, no client - `validate_definition` is a
function of its argument alone, and these tests are what keep it that way.

Assertions are on the CONTENT of the returned strings, not just their
count. The whole contract of this validator is that a person reading its
output can tell what to fix, so a test that only counted errors would pass
against a validator that returned "invalid" three times.
"""

from apps.api.app.strategies.validation import validate_definition

VALID_DEFINITION = {
    "indicators": [{"id": "sma_20", "type": "sma", "period": 20}],
    "entry_rule": {"op": "crosses_above", "left": "sma_20", "right": "close"},
    "exit_rule": {"op": "crosses_below", "left": "sma_20", "right": "close"},
    "position_sizing": {"type": "all_in"},
}


def _with(**overrides) -> dict:
    """A copy of VALID_DEFINITION with some top-level keys replaced, so
    each failure test isolates exactly one broken thing."""
    definition = {key: value for key, value in VALID_DEFINITION.items()}
    definition.update(overrides)
    return definition


def test_the_canonical_valid_definition_produces_no_errors():
    assert validate_definition(VALID_DEFINITION) == []


def test_an_empty_definition_reports_all_four_missing_top_level_keys():
    errors = validate_definition({})
    assert errors == [
        "indicators is required",
        "entry_rule is required",
        "exit_rule is required",
        "position_sizing is required",
    ]


def test_an_unknown_top_level_key_is_rejected_not_ignored():
    errors = validate_definition(_with(entry_rules=VALID_DEFINITION["entry_rule"]))
    assert any(
        "unknown top-level key 'entry_rules'" in error and "allowed keys are:" in error
        for error in errors
    )


def test_indicators_must_be_a_list():
    errors = validate_definition(_with(indicators={"id": "sma_20"}))
    assert "indicators must be a list, got object" in errors


def test_an_indicator_entry_must_be_an_object():
    errors = validate_definition(_with(indicators=["sma_20"]))
    assert "indicators[0] must be a JSON object, got string" in errors


def test_an_indicator_with_an_unknown_key_is_rejected():
    errors = validate_definition(
        _with(indicators=[{"id": "sma_20", "type": "sma", "period": 20, "window": 5}])
    )
    assert any("indicators[0] has unknown key 'window'" in error for error in errors)


def test_a_missing_indicator_id_is_reported():
    errors = validate_definition(_with(indicators=[{"type": "sma", "period": 20}]))
    assert "indicators[0].id is required" in errors


def test_a_blank_indicator_id_is_not_a_valid_id():
    errors = validate_definition(_with(indicators=[{"id": "   ", "type": "sma", "period": 20}]))
    assert any("indicators[0].id must be a non-empty string" in error for error in errors)


def test_an_unknown_indicator_type_names_the_closed_vocabulary():
    errors = validate_definition(_with(indicators=[{"id": "e", "type": "ema", "period": 20}]))
    assert "indicators[0].type 'ema' is not one of: sma, rsi" in errors


def test_a_negative_period_is_reported_with_the_offending_value():
    errors = validate_definition(_with(indicators=[{"id": "sma_20", "type": "sma", "period": -5}]))
    assert "indicators[0].period must be a positive integer, got -5" in errors


def test_a_zero_period_is_not_positive():
    errors = validate_definition(_with(indicators=[{"id": "sma_20", "type": "sma", "period": 0}]))
    assert "indicators[0].period must be a positive integer, got 0" in errors


def test_a_boolean_period_is_not_an_integer_even_though_python_says_it_is():
    errors = validate_definition(
        _with(indicators=[{"id": "sma_20", "type": "sma", "period": True}])
    )
    assert "indicators[0].period must be a positive integer, got True" in errors


def test_a_missing_period_is_reported():
    errors = validate_definition(_with(indicators=[{"id": "sma_20", "type": "sma"}]))
    assert "indicators[0].period is required" in errors


def test_duplicate_indicator_ids_name_both_positions():
    errors = validate_definition(
        _with(
            indicators=[
                {"id": "sma_20", "type": "sma", "period": 20},
                {"id": "sma_20", "type": "sma", "period": 50},
            ]
        )
    )
    assert (
        "indicators[1].id 'sma_20' is already used by indicators[0] - "
        "indicator ids must be unique" in errors
    )


def test_a_rule_must_be_an_object():
    errors = validate_definition(_with(entry_rule=["crosses_above", "sma_20", "close"]))
    assert "entry_rule must be a JSON object, got list" in errors


def test_a_rule_with_an_unknown_key_is_rejected():
    errors = validate_definition(
        _with(entry_rule={"op": "gt", "left": "close", "right": 10, "period": 3})
    )
    assert any("entry_rule has unknown key 'period'" in error for error in errors)


def test_an_unknown_operator_names_the_closed_vocabulary():
    errors = validate_definition(
        _with(entry_rule={"op": "equals", "left": "sma_20", "right": "close"})
    )
    assert (
        "entry_rule.op 'equals' is not one of: "
        "crosses_above, crosses_below, gt, gte, lt, lte" in errors
    )


def test_a_missing_operand_is_reported_per_side():
    errors = validate_definition(_with(exit_rule={"op": "lt"}))
    assert "exit_rule.left is required" in errors
    assert "exit_rule.right is required" in errors


def test_an_operand_referencing_an_undeclared_indicator_is_reported():
    errors = validate_definition(
        _with(entry_rule={"op": "crosses_above", "left": "sma_99", "right": "close"})
    )
    assert "entry_rule.left references undeclared indicator id 'sma_99'" in errors


def test_close_and_a_numeric_threshold_are_both_valid_operands():
    assert (
        validate_definition(
            _with(
                indicators=[{"id": "rsi_14", "type": "rsi", "period": 14}],
                entry_rule={"op": "lt", "left": "rsi_14", "right": 30},
                exit_rule={"op": "gt", "left": "rsi_14", "right": 70.5},
            )
        )
        == []
    )


def test_a_non_string_non_number_operand_is_reported_with_its_type():
    errors = validate_definition(
        _with(entry_rule={"op": "gt", "left": ["close"], "right": "close"})
    )
    assert any(
        "entry_rule.left" in error and "got list" in error for error in errors
    )


def test_a_boolean_operand_is_not_a_numeric_threshold():
    errors = validate_definition(
        _with(entry_rule={"op": "gt", "left": "close", "right": True})
    )
    assert any(
        "entry_rule.right" in error and "got boolean" in error for error in errors
    )


def test_an_undeclared_reference_is_not_reported_when_indicators_itself_is_broken():
    """When `indicators` is not a list at all there is no trustworthy set of
    declared ids, so the rules must not additionally accuse every operand of
    referencing something undeclared - that noise would bury the one real
    error."""
    errors = validate_definition(_with(indicators="sma_20"))
    assert errors == ["indicators must be a list, got string"]


def test_position_sizing_must_be_an_object():
    errors = validate_definition(_with(position_sizing="all_in"))
    assert "position_sizing must be a JSON object, got string" in errors


def test_a_missing_sizing_type_is_reported():
    errors = validate_definition(_with(position_sizing={}))
    assert "position_sizing.type is required" in errors


def test_an_unknown_sizing_type_names_the_closed_vocabulary():
    errors = validate_definition(_with(position_sizing={"type": "martingale"}))
    assert (
        "position_sizing.type 'martingale' is not one of: "
        "all_in, fixed_fraction, fixed_notional" in errors
    )


def test_all_in_takes_no_parameters():
    errors = validate_definition(_with(position_sizing={"type": "all_in", "fraction": 0.5}))
    assert any(
        "position_sizing has unknown key 'fraction' for type 'all_in'" in error
        for error in errors
    )


def test_fixed_fraction_requires_a_fraction():
    errors = validate_definition(_with(position_sizing={"type": "fixed_fraction"}))
    assert "position_sizing.fraction is required for type 'fixed_fraction'" in errors


def test_a_fraction_above_one_is_out_of_range():
    errors = validate_definition(
        _with(position_sizing={"type": "fixed_fraction", "fraction": 1.5})
    )
    assert (
        "position_sizing.fraction must be a number greater than 0 and at most 1, got 1.5"
        in errors
    )


def test_a_zero_fraction_is_out_of_range_but_exactly_one_is_allowed():
    assert (
        "position_sizing.fraction must be a number greater than 0 and at most 1, got 0"
        in validate_definition(
            _with(position_sizing={"type": "fixed_fraction", "fraction": 0})
        )
    )
    assert (
        validate_definition(_with(position_sizing={"type": "fixed_fraction", "fraction": 1}))
        == []
    )


def test_fixed_notional_requires_a_positive_amount():
    assert "position_sizing.amount is required for type 'fixed_notional'" in (
        validate_definition(_with(position_sizing={"type": "fixed_notional"}))
    )
    assert "position_sizing.amount must be a number greater than 0, got -100" in (
        validate_definition(
            _with(position_sizing={"type": "fixed_notional", "amount": -100})
        )
    )
    assert (
        validate_definition(
            _with(position_sizing={"type": "fixed_notional", "amount": 2500})
        )
        == []
    )


def test_it_collects_every_problem_in_one_call_rather_than_stopping_at_the_first():
    """The contract that makes this validator usable from a form: one call
    reports everything wrong, so fixing a five-field mistake is one round
    trip and not five."""
    errors = validate_definition(
        {
            "indicators": [
                {"id": "sma_20", "type": "ema", "period": -5},
                {"id": "sma_20", "type": "sma", "period": 50},
            ],
            "entry_rule": {"op": "equals", "left": "sma_99", "right": "close"},
            "exit_rule": {"op": "crosses_below", "left": "sma_20"},
            "position_sizing": {"type": "fixed_fraction", "fraction": 4},
            "stop_loss": {"type": "percent"},
        }
    )
    expected = {
        "unknown top-level key 'stop_loss' - allowed keys are: "
        "indicators, entry_rule, exit_rule, position_sizing",
        "indicators[0].type 'ema' is not one of: sma, rsi",
        "indicators[0].period must be a positive integer, got -5",
        "indicators[1].id 'sma_20' is already used by indicators[0] - "
        "indicator ids must be unique",
        "entry_rule.op 'equals' is not one of: crosses_above, crosses_below, gt, gte, lt, lte",
        "entry_rule.left references undeclared indicator id 'sma_99'",
        "exit_rule.right is required",
        "position_sizing.fraction must be a number greater than 0 and at most 1, got 4",
    }
    assert expected <= set(errors)
    assert len(errors) >= len(expected)
