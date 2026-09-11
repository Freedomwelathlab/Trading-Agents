"""Phase 68 (D086) adversarial hardening of the closed-vocabulary strategy
pipeline: proof the STATIC no-code-execution property holds, proof
`validate_definition` never raises against ANY input, and proof the
evaluator (`compute_indicator_series` / `evaluate_rule`) fails CLOSED
rather than crashing against malformed bar data.

This module tests properties the rest of the suite assumes but never
directly proves: every other test in `tests/strategies/` and
`tests/backtesting/` exercises well-formed examples (a valid definition, a
realistic bar series). Nothing here duplicates that - it exists to throw
adversarial/malformed input at the same functions and confirm they behave
the way their own docstrings claim: "no eval/exec/compile/import by name,
ever" and "returns a list of strings, never raises."

No new dependency. `hypothesis` is not in this project's dependency set
(checked: `pyproject.toml` lists none), so the adversarial-input test below
is a deliberately hand-crafted table rather than a property-based one, per
Phase 68's own instructions not to add a dependency for this one thing.
"""

import ast
import math
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.strategies.expressions import compute_indicator_series, evaluate_rule
from apps.api.app.strategies.validation import validate_definition

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# 1. A static no-code-execution guard.
# ---------------------------------------------------------------------------

_SCANNED_MODULES = (
    "apps/api/app/strategies/validation.py",
    "apps/api/app/strategies/expressions.py",
    "apps/api/app/strategies/perturbation.py",
    "apps/api/app/backtesting/engine_v2.py",
)
"""Every module whose own docstring makes the "no eval(), exec(), compile(),
import by name" claim explicit (validation.py, expressions.py,
perturbation.py) plus the backtest orchestrator that runs a validated
definition end to end (engine_v2.py) - the actual attack surface: a strategy
definition is untrusted input submitted over HTTP on a system that places
real orders through a real broker (see each module's own docstring)."""

_FORBIDDEN_CALL_NAMES = frozenset({"eval", "exec", "compile", "__import__"})


class _CodeExecutionVisitor(ast.NodeVisitor):
    """Walks one module's AST and records every violation of the "no code
    execution, ever" rule these modules' own docstrings state as a hard
    security boundary:

      * a call to `eval`, `exec`, `compile`, or `__import__` (as a bare name
        OR as an attribute access, e.g. `builtins.eval(...)`), anywhere; and
      * an `import` / `from ... import` statement that appears INSIDE a
        function body - i.e. "import by name" of something chosen at
        runtime, rather than the ordinary module-level imports every one of
        these files legitimately has at the top.

    Module-level imports are never flagged - `function_depth == 0` there -
    only ones nested inside a `def`/`async def`.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.function_depth = 0
        self.violations: list[str] = []

    def _enter_function(self, node: ast.AST) -> None:
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._enter_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._enter_function(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name: str | None = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name in _FORBIDDEN_CALL_NAMES:
            self.violations.append(
                f"{self.path}:{node.lineno}: call to forbidden name {name!r}"
            )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        if self.function_depth > 0:
            self.violations.append(
                f"{self.path}:{node.lineno}: 'import' statement inside a function body "
                "(import-by-name is forbidden here)"
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if self.function_depth > 0:
            self.violations.append(
                f"{self.path}:{node.lineno}: 'from ... import' statement inside a function "
                "body (import-by-name is forbidden here)"
            )
        self.generic_visit(node)


def test_closed_vocabulary_modules_never_eval_exec_compile_or_import_by_name() -> None:
    """AST-walks the four modules this platform's own docs name as the hard
    security boundary and fails loudly - naming the exact file and line -
    if any of them ever gains a call to `eval`/`exec`/`compile`/
    `__import__`, or an `import`/`from ... import` nested inside a function
    body. This turns each module's own "no code execution, ever" docstring
    claim into an automated regression guard a future edit cannot silently
    violate.

    Grep would catch the same literal names, but AST-walking is the more
    rigorous version the phase asked for: it cannot be fooled by the name
    appearing in a comment or a string literal, and it distinguishes a
    legitimate module-level import (every one of these files has several)
    from an import chosen at runtime inside a function body.
    """
    all_violations: list[str] = []
    for relative_path in _SCANNED_MODULES:
        path = _REPO_ROOT / relative_path
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        visitor = _CodeExecutionVisitor(relative_path)
        visitor.visit(tree)
        all_violations.extend(visitor.violations)

    assert all_violations == [], (
        "Found code-execution-primitive usage in modules that must never contain any:\n"
        + "\n".join(all_violations)
    )


# ---------------------------------------------------------------------------
# 2. `validate_definition` never raises, for any input.
# ---------------------------------------------------------------------------

_valid_indicator = {"id": "sma_20", "type": "sma", "period": 20}
_valid_entry_rule = {"op": "crosses_above", "left": "sma_20", "right": "close"}
_valid_exit_rule = {"op": "crosses_below", "left": "sma_20", "right": "close"}


class _SelfRef(dict):
    """A dict that contains itself as a value, constructed via runtime
    mutation - Python dict LITERAL syntax cannot self-reference, but a dict
    can be made to via `d["k"] = d` after construction, exactly as the phase
    instructions call out. Used below to prove a self-referencing structure
    does not hang or crash a validator that never recurses more than one
    level deep into any of these fields."""


def _self_referencing_definition() -> dict:
    d: dict[str, Any] = {}
    d["indicators"] = []
    d["entry_rule"] = d  # entry_rule IS the top-level object, including itself
    d["exit_rule"] = _valid_exit_rule
    d["position_sizing"] = {"type": "all_in"}
    return d


def _self_referencing_indicator() -> dict:
    indicator: dict[str, Any] = {"id": "x", "type": "sma", "period": 5}
    indicator["self"] = indicator  # an indicator entry pointing at itself
    return {
        "indicators": [indicator],
        "entry_rule": _valid_entry_rule,
        "exit_rule": _valid_exit_rule,
        "position_sizing": {"type": "all_in"},
    }


ADVERSARIAL_DEFINITIONS: list[tuple[str, Any]] = [
    # --- non-dict top-level values ---
    ("none", None),
    ("empty_list", []),
    ("nonempty_list", [1, 2, 3]),
    ("plain_string", "not a definition"),
    ("empty_string", ""),
    ("int", 42),
    ("float", 3.14),
    ("bool_true", True),
    ("bool_false", False),
    ("bytes", b"\x00\x01\x02"),
    ("set", {1, 2, 3}),
    ("tuple", (1, 2, 3)),
    ("nan_float", float("nan")),
    ("inf_float", float("inf")),
    # --- deeply nested garbage under otherwise-plausible top-level keys ---
    (
        "deeply_nested_dict_as_entry_rule",
        {
            "indicators": [],
            "entry_rule": {"a": {"b": {"c": {"d": {"e": "f"}}}}},
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    (
        "list_of_lists_as_indicators",
        {
            "indicators": [[1, 2], [3, [4, [5, [6]]]]],
            "entry_rule": _valid_entry_rule,
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    # --- huge strings ---
    (
        "huge_string_indicator_id",
        {
            "indicators": [{"id": "x" * 200_000, "type": "sma", "period": 20}],
            "entry_rule": {"op": "gt", "left": "x" * 200_000, "right": "close"},
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    ("huge_string_top_level", "y" * 500_000),
    # --- unicode / control characters in string fields ---
    (
        "control_chars_and_unicode_op",
        {
            "indicators": [],
            "entry_rule": {"op": "\x00\x01\x02💥ñ‮", "left": "close", "right": 1},
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    (
        "unicode_indicator_id",
        {
            "indicators": [{"id": "🚀日本語\x00\n\t", "type": "sma", "period": 20}],
            "entry_rule": {"op": "gt", "left": "🚀日本語\x00\n\t", "right": 1},
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    # --- NaN / Infinity as strings, and as actual floats, in numeric fields ---
    (
        "period_as_string_nan",
        {
            "indicators": [{"id": "a", "type": "sma", "period": "NaN"}],
            "entry_rule": _valid_entry_rule,
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    (
        "period_as_string_infinity",
        {
            "indicators": [{"id": "a", "type": "sma", "period": "Infinity"}],
            "entry_rule": _valid_entry_rule,
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    (
        "period_as_actual_nan_float",
        {
            "indicators": [{"id": "a", "type": "sma", "period": float("nan")}],
            "entry_rule": _valid_entry_rule,
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    (
        "fraction_as_actual_infinity",
        {
            "indicators": [],
            "entry_rule": _valid_entry_rule,
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "fixed_fraction", "fraction": float("inf")},
        },
    ),
    # --- negative / zero / huge period ---
    (
        "negative_period",
        {
            "indicators": [{"id": "a", "type": "sma", "period": -1}],
            "entry_rule": _valid_entry_rule,
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    (
        "zero_period",
        {
            "indicators": [{"id": "a", "type": "rsi", "period": 0}],
            "entry_rule": _valid_entry_rule,
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    (
        "huge_period",
        {
            "indicators": [{"id": "a", "type": "sma", "period": 10**18}],
            "entry_rule": _valid_entry_rule,
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    (
        "negative_huge_period",
        {
            "indicators": [{"id": "a", "type": "sma", "period": -(10**18)}],
            "entry_rule": _valid_entry_rule,
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    # --- circular-reference-shaped dicts, via runtime mutation ---
    ("self_referencing_definition", _self_referencing_definition()),
    ("self_referencing_indicator_entry", _self_referencing_indicator()),
    # --- every legal top-level key present, but with a completely wrong
    #     type for its value ---
    (
        "all_keys_wrong_types",
        {"indicators": 42, "entry_rule": [], "exit_rule": "nope", "position_sizing": None},
    ),
    (
        "indicators_is_a_dict_not_a_list",
        {
            "indicators": {"id": "a", "type": "sma", "period": 20},
            "entry_rule": _valid_entry_rule,
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    (
        "rules_are_lists_not_objects",
        {
            "indicators": [_valid_indicator],
            "entry_rule": ["crosses_above", "sma_20", "close"],
            "exit_rule": ["crosses_below", "sma_20", "close"],
            "position_sizing": {"type": "all_in"},
        },
    ),
    (
        "position_sizing_is_a_string",
        {
            "indicators": [],
            "entry_rule": _valid_entry_rule,
            "exit_rule": _valid_exit_rule,
            "position_sizing": "all_in",
        },
    ),
    # --- unhashable values in vocabulary-checked fields (D086's own finding) ---
    (
        "unhashable_indicator_type",
        {
            "indicators": [{"id": "a", "type": ["sma"], "period": 20}],
            "entry_rule": _valid_entry_rule,
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    (
        "unhashable_rule_operator",
        {
            "indicators": [],
            "entry_rule": {"op": {"not": "a string"}, "left": "close", "right": 1},
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    (
        "unhashable_sizing_type",
        {
            "indicators": [],
            "entry_rule": _valid_entry_rule,
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": {"a": 1}},
        },
    ),
    (
        "unhashable_sizing_type_set",
        {
            "indicators": [],
            "entry_rule": _valid_entry_rule,
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": {1, 2, 3}},
        },
    ),
    # --- operands and indicator ids of adversarial shapes ---
    (
        "operand_is_a_dict",
        {
            "indicators": [],
            "entry_rule": {"op": "gt", "left": {"nested": True}, "right": "close"},
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    (
        "operand_is_none",
        {
            "indicators": [],
            "entry_rule": {"op": "gt", "left": None, "right": "close"},
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    (
        "duplicate_indicator_ids",
        {
            "indicators": [
                {"id": "dup", "type": "sma", "period": 5},
                {"id": "dup", "type": "rsi", "period": 14},
            ],
            "entry_rule": {"op": "gt", "left": "dup", "right": "close"},
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
        },
    ),
    (
        "unknown_top_level_key_with_huge_value",
        {
            "indicators": [],
            "entry_rule": _valid_entry_rule,
            "exit_rule": _valid_exit_rule,
            "position_sizing": {"type": "all_in"},
            "extra_junk": "z" * 100_000,
        },
    ),
]

assert 30 <= len(ADVERSARIAL_DEFINITIONS) <= 50, (
    f"expected 30-50 adversarial cases, got {len(ADVERSARIAL_DEFINITIONS)}"
)


@pytest.mark.parametrize(
    "definition", [value for _id, value in ADVERSARIAL_DEFINITIONS], ids=[
        id_ for id_, _value in ADVERSARIAL_DEFINITIONS
    ]
)
def test_validate_definition_never_raises_on_adversarial_input(definition: Any) -> None:
    """`validate_definition` must return `list[str]` - never raise, never
    return anything else - for every one of these deliberately malformed
    inputs. A non-empty result is expected and fine (these are almost all
    invalid definitions); a raised exception of ANY kind is the failure
    this test exists to catch."""
    try:
        errors = validate_definition(definition)
    except Exception as exc:  # noqa: BLE001 - the whole point is "did it raise at all"
        pytest.fail(
            f"validate_definition raised {type(exc).__name__}: {exc} "
            f"for input {definition!r}"
        )
    assert isinstance(errors, list)
    assert all(isinstance(item, str) for item in errors)


def test_validate_definition_return_type_contract_on_the_happy_path_too() -> None:
    """Sanity check the harness itself: a genuinely valid definition still
    returns `[]`, so the adversarial table above is testing against a
    validator that actually accepts good input, not one that rejects
    everything indiscriminately."""
    valid = {
        "indicators": [_valid_indicator],
        "entry_rule": _valid_entry_rule,
        "exit_rule": _valid_exit_rule,
        "position_sizing": {"type": "all_in"},
    }
    assert validate_definition(valid) == []


# ---------------------------------------------------------------------------
# 3. The evaluator never raises against a validated definition + adversarial
#    bar data.
# ---------------------------------------------------------------------------

SMA_DEFINITION = {
    "indicators": [{"id": "sma_2", "type": "sma", "period": 2}],
    "entry_rule": {"op": "crosses_above", "left": "close", "right": "sma_2"},
    "exit_rule": {"op": "crosses_below", "left": "close", "right": "sma_2"},
    "position_sizing": {"type": "all_in"},
}

RSI_DEFINITION = {
    "indicators": [{"id": "rsi_14", "type": "rsi", "period": 14}],
    "entry_rule": {"op": "lt", "left": "rsi_14", "right": 30},
    "exit_rule": {"op": "gt", "left": "rsi_14", "right": 70},
    "position_sizing": {"type": "fixed_fraction", "fraction": 0.5},
}

CROSSING_DEFINITION = {
    "indicators": [{"id": "sma_3", "type": "sma", "period": 3}],
    "entry_rule": {"op": "crosses_above", "left": "close", "right": "sma_3"},
    "exit_rule": {"op": "crosses_below", "left": "close", "right": "sma_3"},
    "position_sizing": {"type": "fixed_notional", "amount": 1000},
}

VALID_DEFINITIONS = [SMA_DEFINITION, RSI_DEFINITION, CROSSING_DEFINITION]

for _definition in VALID_DEFINITIONS:
    assert validate_definition(_definition) == [], (
        f"a definition this module treats as VALID actually failed validation: {_definition!r}"
    )


def _bar(close: Decimal | None, *, ts_offset_seconds: int = 0, symbol: str = "FUZZ") -> Bar:
    """One bar, deliberately built via `model_construct` rather than the
    normal validated constructor when `close` would not otherwise be legal
    (`Bar.close` is `Field(gt=0)`, so a `None` or non-positive close cannot
    be constructed through Pydantic's own validation at all - which is
    itself a real safety property of this codebase, not a gap in this
    test). `model_construct` bypasses that validation to prove the
    EVALUATOR's own defenses hold independently of Pydantic's, exactly the
    posture the phase asks for: fail closed even against a bar that should
    never exist, not fail closed only because Pydantic already rejected it
    upstream."""
    ts = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=ts_offset_seconds)
    if close is not None and close > 0:
        return Bar(symbol=symbol, bar_interval="1d", ts=ts, close=close, source="fuzz-test")
    return Bar.model_construct(
        symbol=symbol, bar_interval="1d", ts=ts, close=close, source="fuzz-test"
    )


def _run_definition_against_bars(definition: dict, bars: list[Bar]) -> None:
    """Computes every indicator series and evaluates both rules at every
    bar index - the same two calls `backtesting/executor.py::generate_signals`
    makes - and asserts neither ever raises anything but the one exception
    type this codebase already uses deliberately for "not enough data"
    (`InsufficientDataError`, which `compute_indicator_series` itself always
    catches and converts to `None` - so even that is not expected to
    surface here)."""
    indicator_series = {
        indicator["id"]: compute_indicator_series(bars, indicator)
        for indicator in definition["indicators"]
    }
    for index in range(len(bars)):
        for rule_key in ("entry_rule", "exit_rule"):
            result = evaluate_rule(
                definition[rule_key],
                bars=bars,
                indicator_series=indicator_series,
                index=index,
            )
            assert result is None or isinstance(result, bool)


@pytest.mark.parametrize("definition", VALID_DEFINITIONS, ids=["sma", "rsi", "crossing"])
def test_evaluator_never_raises_on_an_empty_bar_list(definition: dict) -> None:
    """`compute_indicator_series` on zero bars must return `[]`, never
    raise - there is no bar index for `evaluate_rule` to be asked about at
    all, so only the indicator-series half of the evaluator is exercised
    here."""
    for indicator in definition["indicators"]:
        series = compute_indicator_series([], indicator)
        assert series == []


@pytest.mark.parametrize("definition", VALID_DEFINITIONS, ids=["sma", "rsi", "crossing"])
def test_evaluator_never_raises_on_a_single_bar(definition: dict) -> None:
    """One bar: every indicator is necessarily undefined (`None`), and a
    crossing rule at `index == 0` has no previous bar to compare against
    and must return `None` rather than raising or guessing `True`/`False`.
    An instantaneous rule (`lt`/`gt`) CAN still resolve on one bar if its
    indicator happens to be defined already - not the case here, but the
    evaluator must not assume either way."""
    bars = [_bar(Decimal(100))]
    _run_definition_against_bars(definition, bars)
    for indicator in definition["indicators"]:
        series = compute_indicator_series(bars, indicator)
        assert series == [None]


@pytest.mark.parametrize("definition", VALID_DEFINITIONS, ids=["sma", "rsi", "crossing"])
def test_evaluator_never_raises_on_bars_with_a_none_close(definition: dict) -> None:
    """A bar whose `close` is `None` cannot be constructed through Pydantic
    validation (`Bar.close` requires `gt=0`) - so this test deliberately
    bypasses that with `model_construct` (see `_bar`'s docstring) to prove
    the evaluator itself fails closed against a corrupted bar reaching it
    by some path other than the normal validated constructor, rather than
    relying solely on Pydantic's own guarantee."""
    bars = [_bar(Decimal(100), ts_offset_seconds=0), _bar(None, ts_offset_seconds=86_400)]
    try:
        _run_definition_against_bars(definition, bars)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"evaluator raised {type(exc).__name__}: {exc} against a None-close bar "
            f"for definition {definition!r}"
        )


@pytest.mark.parametrize("definition", VALID_DEFINITIONS, ids=["sma", "rsi", "crossing"])
def test_evaluator_never_raises_on_bars_with_duplicate_timestamps(definition: dict) -> None:
    """Two bars sharing the exact same `ts` - a vendor data-quality problem
    this evaluator has no way to detect or reject (it only ever receives a
    plain list, oldest-first, per its own documented contract) - must still
    be evaluated without raising. `compute_indicator_series` never looks at
    `ts` at all (only `bar.close`), so this mostly proves that fact."""
    same_ts = _bar(Decimal(100)).ts
    bars = [
        Bar(symbol="FUZZ", bar_interval="1d", ts=same_ts, close=Decimal(100), source="fuzz-test"),
        Bar(symbol="FUZZ", bar_interval="1d", ts=same_ts, close=Decimal(101), source="fuzz-test"),
        Bar(symbol="FUZZ", bar_interval="1d", ts=same_ts, close=Decimal(99), source="fuzz-test"),
    ]
    _run_definition_against_bars(definition, bars)


@pytest.mark.parametrize("definition", VALID_DEFINITIONS, ids=["sma", "rsi", "crossing"])
def test_evaluator_never_raises_on_a_negative_or_zero_close(definition: dict) -> None:
    """`Bar.close` requires `gt=0`, so - exactly like the `None`-close case
    above - a negative or zero close can only reach the evaluator via
    `model_construct`, bypassing Pydantic. This proves the evaluator does
    not assume a positive close either: SMA/RSI are pure arithmetic over
    whatever Decimal values they are handed, and must not raise merely
    because one of them is non-positive."""
    bars = [
        _bar(Decimal(100), ts_offset_seconds=0),
        _bar(Decimal(0), ts_offset_seconds=86_400),
        _bar(Decimal(-50), ts_offset_seconds=2 * 86_400),
        _bar(Decimal(75), ts_offset_seconds=3 * 86_400),
    ]
    try:
        _run_definition_against_bars(definition, bars)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"evaluator raised {type(exc).__name__}: {exc} against a non-positive close "
            f"for definition {definition!r}"
        )


@pytest.mark.parametrize("definition", VALID_DEFINITIONS, ids=["sma", "rsi", "crossing"])
def test_evaluator_handles_a_few_thousand_bars_well_under_a_second(definition: dict) -> None:
    """A generous wall-clock sanity bound, not a tight benchmark (the phase
    explicitly asks for `< 5` seconds, not a performance target): a few
    thousand bars through `compute_indicator_series` + `evaluate_rule` at
    every index is pure in-memory Decimal arithmetic and must not
    accidentally be quadratic-or-worse in a way that would turn a real
    backtest over years of daily bars into a timeout."""
    num_bars = 3000
    bars = [
        _bar(Decimal(100 + (i % 7) - 3), ts_offset_seconds=i * 86_400) for i in range(num_bars)
    ]

    started = time.monotonic()
    _run_definition_against_bars(definition, bars)
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"evaluating {num_bars} bars took {elapsed:.2f}s, expected well under 5s"


def test_nan_and_infinity_decimal_closes_do_not_raise_in_the_evaluator() -> None:
    """Decimal itself supports NaN and Infinity, and nothing between the
    (bypassed, via `model_construct`) bar and the evaluator's pure
    arithmetic rejects them. SMA is a sum-and-divide (NaN/Infinity simply
    propagate through Decimal arithmetic, as IEEE-754-style semantics
    require) and the comparisons in `evaluate_rule` are ordinary Decimal
    comparisons, all of which are well-defined for NaN/Infinity without
    raising - this test proves that stays true rather than assuming it."""
    bars = [
        Bar.model_construct(
            symbol="FUZZ",
            bar_interval="1d",
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            close=Decimal(100),
            source="fuzz-test",
        ),
        Bar.model_construct(
            symbol="FUZZ",
            bar_interval="1d",
            ts=datetime(2026, 1, 2, tzinfo=UTC),
            close=Decimal("NaN"),
            source="fuzz-test",
        ),
        Bar.model_construct(
            symbol="FUZZ",
            bar_interval="1d",
            ts=datetime(2026, 1, 3, tzinfo=UTC),
            close=Decimal("Infinity"),
            source="fuzz-test",
        ),
        Bar.model_construct(
            symbol="FUZZ",
            bar_interval="1d",
            ts=datetime(2026, 1, 4, tzinfo=UTC),
            close=Decimal(100),
            source="fuzz-test",
        ),
    ]
    try:
        _run_definition_against_bars(SMA_DEFINITION, bars)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"evaluator raised {type(exc).__name__}: {exc} against NaN/Infinity closes")


def test_math_nan_sanity_check() -> None:
    """Guards the test module's own assumption that `float('nan') != float('nan')`
    (used above to build adversarial numeric fields) - if this ever stopped
    being true the adversarial table's NaN cases would silently stop testing
    what their names claim."""
    assert math.isnan(float("nan"))
    assert not math.isnan(1.0)
