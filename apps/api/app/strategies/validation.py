"""Purely STRUCTURAL validation of a strategy definition (Phase 54).

**No code execution, ever.** This module does not `eval()`, `exec()`,
`compile()`, import by name, or otherwise turn any part of a user-supplied
definition into something that runs. It reads a plain JSON structure and
compares it against the closed vocabulary in
apps/api/app/strategies/models.py. That is a hard security boundary on this
platform, not a stylistic preference: a strategy definition is untrusted
input submitted over HTTP by whoever holds `strategy:manage`, and this is a
system that places real orders through a real broker. A definition language
that could execute arbitrary expressions would put remote code execution
one endpoint away from the trading path. Phase 55's evaluator inherits the
same rule - it walks this same closed vocabulary and dispatches to
deterministic functions; it never compiles anything either.

Nothing here reads a price, opens a session, calls a vendor, or touches the
bar store. Validation of a definition is answerable from the definition
alone, so it is - and must stay - a pure function of its argument.

**Every problem, not the first one.** `validate_definition` returns a list
of concrete, itemized, human-readable strings and never stops at the first
failure. A builder UI (and a person) needs to see everything that is wrong
in one pass; a validator that reports "invalid" and quits turns fixing a
five-field form into five round trips. An empty list - and only an empty
list - means valid.

Messages name the exact path (`indicators[1].period`,
`position_sizing.type`) and quote the offending value, so an error is
actionable without the reader having to re-derive which of three
indicators the complaint is about.
"""

import enum
from typing import Any

from apps.api.app.strategies.models import (
    DEFINITION_KEYS,
    INDICATOR_KEYS,
    PRICE_OPERAND_CLOSE,
    REQUIRED_SIZING_KEYS,
    RULE_KEYS,
    IndicatorType,
    PositionSizingType,
    RuleOperator,
)


def _type_name(value: Any) -> str:
    """The JSON-ish name of a value's type, for error messages - `list`
    rather than Python's `list` vs `tuple` distinction, which a caller
    sending JSON cannot act on."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_number(value: Any) -> bool:
    """JSON numbers only. `bool` is excluded even though Python makes it a
    subclass of `int` - `true` is not a threshold, and accepting it would
    silently turn `{"right": true}` into a comparison against 1."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _vocabulary(enum_cls: type[enum.Enum]) -> str:
    """The allowed values of a closed vocabulary, in declaration order -
    which is the order they are documented and thought about in, not
    alphabetical order that would scramble e.g. gt/gte/lt/lte."""
    return ", ".join(str(member.value) for member in enum_cls)


def _check_keys(
    errors: list[str], obj: dict[str, Any], allowed: tuple[str, ...], where: str
) -> None:
    """Unknown keys are errors, not ignored input. The overwhelmingly
    common cause is a typo, and ignoring it would leave the definition
    validating cleanly while doing something other than what was written."""
    for key in obj:
        if key not in allowed:
            errors.append(
                f"{where} has unknown key {key!r} - allowed keys are: {', '.join(allowed)}"
            )


def _validate_indicators(errors: list[str], raw: Any) -> tuple[set[str], bool]:
    """Returns the set of declared indicator ids and whether that set can
    be trusted as complete. When `indicators` is missing or is not a list
    at all, the set is unusable and rule operands must not be checked
    against it - otherwise every rule in the definition would additionally
    report a bogus "undeclared indicator" on top of the real error."""
    if not isinstance(raw, list):
        errors.append(f"indicators must be a list, got {_type_name(raw)}")
        return set(), False

    declared: dict[str, int] = {}
    for index, entry in enumerate(raw):
        where = f"indicators[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be a JSON object, got {_type_name(entry)}")
            continue

        _check_keys(errors, entry, INDICATOR_KEYS, where)

        if "id" not in entry:
            errors.append(f"{where}.id is required")
        elif not isinstance(entry["id"], str) or not entry["id"].strip():
            errors.append(
                f"{where}.id must be a non-empty string, got {entry['id']!r}"
            )
        else:
            indicator_id = entry["id"]
            if indicator_id in declared:
                errors.append(
                    f"{where}.id {indicator_id!r} is already used by "
                    f"indicators[{declared[indicator_id]}] - indicator ids must be unique"
                )
            else:
                declared[indicator_id] = index

        if "type" not in entry:
            errors.append(f"{where}.type is required")
        elif entry["type"] not in {member.value for member in IndicatorType}:
            errors.append(
                f"{where}.type {entry['type']!r} is not one of: {_vocabulary(IndicatorType)}"
            )

        if "period" not in entry:
            errors.append(f"{where}.period is required")
        elif not _is_positive_int(entry["period"]):
            errors.append(
                f"{where}.period must be a positive integer, got {entry['period']!r}"
            )

    return set(declared), True


def _validate_operand(
    errors: list[str], value: Any, where: str, declared: set[str], declared_usable: bool
) -> None:
    """An operand is the literal string "close", a declared indicator's id,
    or a JSON number used as a fixed threshold. Nothing else - notably not
    a nested expression: the rule shape is deliberately flat this phase,
    so there is no recursion here to bound and no depth limit to get
    wrong."""
    if _is_number(value):
        return
    if not isinstance(value, str):
        errors.append(
            f'{where} must be "{PRICE_OPERAND_CLOSE}", a declared indicator id, or a '
            f"number, got {_type_name(value)}"
        )
        return
    if value == PRICE_OPERAND_CLOSE:
        return
    if declared_usable and value not in declared:
        errors.append(f"{where} references undeclared indicator id {value!r}")


def _validate_rule(
    errors: list[str], raw: Any, where: str, declared: set[str], declared_usable: bool
) -> None:
    if not isinstance(raw, dict):
        errors.append(f"{where} must be a JSON object, got {_type_name(raw)}")
        return

    _check_keys(errors, raw, RULE_KEYS, where)

    if "op" not in raw:
        errors.append(f"{where}.op is required")
    elif raw["op"] not in {member.value for member in RuleOperator}:
        errors.append(f"{where}.op {raw['op']!r} is not one of: {_vocabulary(RuleOperator)}")

    for side in ("left", "right"):
        if side not in raw:
            errors.append(f"{where}.{side} is required")
            continue
        _validate_operand(errors, raw[side], f"{where}.{side}", declared, declared_usable)


def _validate_position_sizing(errors: list[str], raw: Any) -> None:
    where = "position_sizing"
    if not isinstance(raw, dict):
        errors.append(f"{where} must be a JSON object, got {_type_name(raw)}")
        return

    if "type" not in raw:
        errors.append(f"{where}.type is required")
        return
    if raw["type"] not in {member.value for member in PositionSizingType}:
        errors.append(
            f"{where}.type {raw['type']!r} is not one of: {_vocabulary(PositionSizingType)}"
        )
        return

    sizing_type = PositionSizingType(raw["type"])
    required = REQUIRED_SIZING_KEYS[sizing_type]
    allowed = ("type", *required)
    for key in raw:
        if key not in allowed:
            errors.append(
                f"{where} has unknown key {key!r} for type {sizing_type.value!r} - "
                f"allowed keys are: {', '.join(allowed)}"
            )

    if sizing_type is PositionSizingType.FIXED_FRACTION:
        if "fraction" not in raw:
            errors.append(f"{where}.fraction is required for type 'fixed_fraction'")
        elif not _is_number(raw["fraction"]) or not (0 < raw["fraction"] <= 1):
            errors.append(
                f"{where}.fraction must be a number greater than 0 and at most 1, "
                f"got {raw['fraction']!r}"
            )
    elif sizing_type is PositionSizingType.FIXED_NOTIONAL:
        if "amount" not in raw:
            errors.append(f"{where}.amount is required for type 'fixed_notional'")
        elif not _is_number(raw["amount"]) or raw["amount"] <= 0:
            errors.append(
                f"{where}.amount must be a number greater than 0, got {raw['amount']!r}"
            )


def validate_definition(definition: dict) -> list[str]:
    """Every structural problem with `definition`, as concrete strings.

    An empty list means the definition is well formed against the closed
    vocabulary in apps/api/app/strategies/models.py. It does NOT mean the
    strategy is any good, profitable, or safe - this function makes no
    claim about a strategy's merit and is not a substitute for a backtest.
    It means only that the rules are expressible and internally consistent
    (every referenced indicator is declared, every operator is known, every
    sizing parameter is present and in range).

    An empty `{}` is a legal draft to STORE - a strategy is created before
    it is written - but it is not a valid definition, so this reports the
    four missing top-level keys rather than passing it.
    """
    errors: list[str] = []

    if not isinstance(definition, dict):
        return [f"definition must be a JSON object, got {_type_name(definition)}"]

    for key in definition:
        if key not in DEFINITION_KEYS:
            errors.append(
                f"unknown top-level key {key!r} - allowed keys are: "
                f"{', '.join(DEFINITION_KEYS)}"
            )
    for key in DEFINITION_KEYS:
        if key not in definition:
            errors.append(f"{key} is required")

    declared: set[str] = set()
    declared_usable = False
    if "indicators" in definition:
        declared, declared_usable = _validate_indicators(errors, definition["indicators"])

    for rule_key in ("entry_rule", "exit_rule"):
        if rule_key in definition:
            _validate_rule(errors, definition[rule_key], rule_key, declared, declared_usable)

    if "position_sizing" in definition:
        _validate_position_sizing(errors, definition["position_sizing"])

    return errors
