"""Deterministic parameter perturbation of a StrategyDefinition (Phase 58).

**What this is for.** Classic one-factor-at-a-time sensitivity analysis. Take
a validated definition and, for each NUMERIC parameter it declares, produce
two variants: that one parameter nudged up by a fixed magnitude, and nudged
down, with every other parameter held exactly at its original value. A
strategy whose backtest result barely moves across those nudges is more
likely to be robust; one whose result swings wildly from a 10% change in a
single period is showing the classic symptom of a curve fit to one
particular history. Neither conclusion is drawn here - this module only
enumerates the variants.

**This module is a JSON transformer and nothing else.** It runs no
backtest, opens no session, reads no bar, calls no vendor, and touches no
historical data. It takes a dict and returns dicts. The orchestrator that
consumes this output is what replays each variant through the backtest
engine and reports the spread.

**No code execution, ever.** The same hard rule
apps/api/app/strategies/validation.py states applies verbatim: nothing here
`eval()`s, `exec()`s, `compile()`s, or imports by name. A definition is
untrusted input submitted over HTTP on a system that places real orders
through a real broker. This module reads two known keys off a plain JSON
structure (`indicators[i].period` and the `position_sizing` parameter named
by the closed vocabulary in apps/api/app/strategies/models.py), does Decimal
arithmetic on them, and writes the result back. The only strings it ever
interprets are ones already checked against those enums.

**No randomness, anywhere.** Two calls with the same arguments produce
byte-identical output in the same order, always. Sampling a parameter space
stochastically is Monte Carlo's job and belongs in its own module; a
robustness report that could not be reproduced from its inputs would not be
evidence of anything.

**Caller validates, callee trusts.** `generate_perturbed_definitions`
assumes `validate_definition(definition) == []` and does not re-check it -
the same convention `backtesting/executor.py::generate_signals` adopted in
Phase 55, for the same reason: one enforcement point cannot disagree with
itself, two can. A definition that never passed validation may raise
`KeyError`/`TypeError` here rather than producing a diagnosis, which is the
intended failure - this module is not a second validator.

**All arithmetic is Decimal.** `Decimal(str(value))`, never
`Decimal(float)`: `Decimal(0.1)` is the binary float
0.1000000000000000055..., while `Decimal("0.1")` is the number the user
actually wrote. This is the same conversion
`backtesting/engine_v2.py::_desired_quantity` already uses when it reads
these same sizing parameters. A binary float is never allowed to touch a
perturbed value, because a "10% nudge" that silently also moved the 17th
decimal place would make two runs incomparable for a reason having nothing
to do with the strategy.

**Type of the values written back.** `indicators[i].period` is written back
as a plain `int`, matching `validation.py::_is_positive_int` and how every
other module in this package reads it. The sizing parameters `fraction` and
`amount` are written back as exact `Decimal`s, which is what keeps the
arithmetic above exact. Note the consequence, deliberately not hidden:
`validation.py::_is_number` admits only `int`/`float`, so a perturbed
`fixed_fraction`/`fixed_notional` definition will NOT re-validate as-is, and
is not JSON-serializable without a Decimal-aware encoder. A caller that
needs either must convert at that boundary and decide there what precision
loss it will accept; this module refuses to make that choice silently by
handing back a float. Perturbed `period` definitions have no such caveat and
re-validate cleanly.
"""

import copy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from apps.api.app.strategies.models import PositionSizingType

_DIRECTIONS: tuple[str, ...] = ("-", "+")
"""Both directions of every nudge, in the order they are emitted. Down
first, purely so the output order is fixed and documented rather than
incidental."""

_MIN_PERIOD = Decimal(1)
"""The floor for an indicator period. `validation.py::_is_positive_int`
requires a positive integer, so 0 and negatives are not merely useless
values - they are values no definition may legally hold."""

_POSITIVE_FLOOR = Decimal("0.01")
"""The floor for `fraction` and `amount`, both of which the closed
vocabulary requires to be strictly greater than zero. Clamping to 0 instead
would generate a definition that could never pass validation and could never
be backtested, which tests nothing; 1% is the smallest round positive value
that stays inside both parameters' legal range."""

_FRACTION_MAX = Decimal(1)
"""`position_sizing.fraction` lives in (0, 1] - see
`validation.py::_validate_position_sizing`. An upward nudge that would put
100% of equity into a fraction above 1 is clamped here rather than emitted
as an invalid definition."""


@dataclass(frozen=True)
class Perturbation:
    """One variant: exactly one parameter moved, everything else identical.

    `definition` is a full, standalone StrategyDefinition - a deep copy of
    the original with this one leaf changed - so the consumer can hand it
    straight to a backtest without reassembling anything, and nothing it
    does to the variant can reach the caller's original.

    `original_value` and `perturbed_value` are both `Decimal` even for an
    integer period, so a report can compute a percentage change across
    parameters of different kinds without special-casing which parameter it
    is looking at. The value written INTO `definition` keeps that
    parameter's own JSON type (see the module docstring).

    `clamped` says the perturbed value hit a boundary of the parameter's
    legal range instead of landing on the raw magnitude-adjusted value. It
    matters when reading results: a clamped variant moved by less (or, at a
    floor, differently) than the nominal magnitude, so "the result barely
    changed" is weaker evidence of robustness there than it looks.
    """

    parameter_path: str
    direction: str
    original_value: Decimal
    perturbed_value: Decimal
    definition: dict
    clamped: bool


def _to_decimal(value: Any) -> Decimal:
    """A JSON number (or an already-exact Decimal) as an exact Decimal.

    Via `str` on purpose - `Decimal(0.1)` is a binary float artifact,
    `Decimal("0.1")` is the number that was written. Identical to the
    conversion `backtesting/engine_v2.py` performs on these same fields.
    """
    return Decimal(str(value))


def _factor(magnitude_pct: Decimal, direction: str) -> Decimal:
    """`1 + magnitude_pct/100` for "+", `1 - magnitude_pct/100` for "-".

    `magnitude_pct` is a plain percentage: `Decimal("10")` means ±10%.
    """
    step = magnitude_pct / Decimal(100)
    return Decimal(1) + step if direction == "+" else Decimal(1) - step


def _perturb_period(
    definition: dict, index: int, direction: str, magnitude_pct: Decimal
) -> Perturbation | None:
    """One nudge of `indicators[index].period`, or `None` to skip it.

    The magnitude-adjusted value is rounded to a whole number with Python's
    `round()` on the Decimal - i.e. banker's rounding, half to even - because
    a period is a bar count and there is no such thing as a 19.8-bar moving
    average. The whole computation happens in Decimal and only the final
    integer leaves it.

    `None` means this direction would not actually perturb anything: the
    rounded (and possibly floored) value came back equal to the original, so
    the variant would be the original definition wearing a different label.
    Emitting it would report a 0% change in the backtest result and read as
    evidence of robustness, when in fact nothing was tested.
    """
    indicator = definition["indicators"][index]
    original = _to_decimal(indicator["period"])

    rounded = Decimal(round(original * _factor(magnitude_pct, direction)))
    clamped = rounded < _MIN_PERIOD
    perturbed = _MIN_PERIOD if clamped else rounded
    if perturbed == original:
        return None

    variant = copy.deepcopy(definition)
    variant["indicators"][index]["period"] = int(perturbed)
    return Perturbation(
        parameter_path=f"indicators[{index}].period",
        direction=direction,
        original_value=original,
        perturbed_value=perturbed,
        definition=variant,
        clamped=clamped,
    )


def _perturb_sizing(
    definition: dict,
    key: str,
    direction: str,
    magnitude_pct: Decimal,
    *,
    upper: Decimal | None,
) -> Perturbation | None:
    """One nudge of `position_sizing[key]`, or `None` to skip it.

    `upper` is the parameter's inclusive ceiling (`1` for `fraction`, absent
    for `amount`, which the vocabulary bounds only from below). Both
    parameters are floored at `_POSITIVE_FLOOR` rather than at zero, because
    zero is not a legal value for either one.

    The floor is applied to any result at or below zero, which a large enough
    magnitude will produce (a 120% downward nudge is arithmetically
    negative). For an original already smaller than the floor, that clamp
    moves the value UP even in the "-" direction - a consequence of the
    parameter's own legal range, not a sign error, and `clamped` is set so
    the report can say so.

    `None` has the same meaning as in `_perturb_period`: the clamp landed
    back on the original value, so this direction perturbs nothing.
    """
    original = _to_decimal(definition["position_sizing"][key])

    raw = original * _factor(magnitude_pct, direction)
    clamped = False
    perturbed = raw
    if perturbed <= 0:
        perturbed, clamped = _POSITIVE_FLOOR, True
    elif upper is not None and perturbed > upper:
        perturbed, clamped = upper, True
    if perturbed == original:
        return None

    variant = copy.deepcopy(definition)
    variant["position_sizing"][key] = perturbed
    return Perturbation(
        parameter_path=f"position_sizing.{key}",
        direction=direction,
        original_value=original,
        perturbed_value=perturbed,
        definition=variant,
        clamped=clamped,
    )


def generate_perturbed_definitions(
    definition: dict, *, magnitude_pct: Decimal
) -> list[Perturbation]:
    """Every one-factor-at-a-time variant of `definition`, in a fixed order.

    Precondition: `validate_definition(definition) == []`. It is not
    re-checked here (see the module docstring).

    `magnitude_pct` is a plain percentage - `Decimal("10")` for ±10%.

    Perturbable parameters, and only these:

      * every `indicators[i].period`, floored at 1 and rounded to a whole
        number of bars;
      * `position_sizing.fraction`, when the sizing type is
        `fixed_fraction`, clamped into (0, 1];
      * `position_sizing.amount`, when the sizing type is `fixed_notional`,
        clamped to a positive floor.

    `all_in` sizing has no numeric parameter at all and contributes nothing.

    Order is stable and deterministic: `indicators` in declared list order,
    each emitting `-` then `+`, then `position_sizing`, again `-` then `+`.
    Directions that would not change the parameter's value are omitted
    entirely rather than emitted as no-ops, so a definition can produce one
    perturbation for a parameter instead of two.

    **An empty list is a valid, expected answer, not an error.** A
    definition with no indicators and `all_in` sizing - rules written purely
    against `"close"` and fixed numbers - has no numeric parameter to
    perturb, and is a perfectly real strategy. What that means for a
    robustness report (skip it, record it as not-applicable, refuse to score
    it) is the caller's decision to make and state; this function has no
    opinion and raises nothing.
    """
    perturbations: list[Perturbation] = []

    for index in range(len(definition["indicators"])):
        for direction in _DIRECTIONS:
            candidate = _perturb_period(definition, index, direction, magnitude_pct)
            if candidate is not None:
                perturbations.append(candidate)

    sizing_type = PositionSizingType(definition["position_sizing"]["type"])
    key: str | None = None
    upper: Decimal | None = None
    if sizing_type is PositionSizingType.FIXED_FRACTION:
        key, upper = "fraction", _FRACTION_MAX
    elif sizing_type is PositionSizingType.FIXED_NOTIONAL:
        key, upper = "amount", None

    if key is not None:
        for direction in _DIRECTIONS:
            candidate = _perturb_sizing(
                definition, key, direction, magnitude_pct, upper=upper
            )
            if candidate is not None:
                perturbations.append(candidate)

    return perturbations
