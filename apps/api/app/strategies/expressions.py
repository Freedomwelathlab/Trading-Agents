"""Evaluation primitives for a validated StrategyDefinition (Phase 55).

**No code execution, ever.** This module inherits, verbatim, the hard rule
apps/api/app/strategies/validation.py states: it does not `eval()`,
`exec()`, `compile()`, import by name, or otherwise turn any part of a
user-supplied definition into something that runs. What follows is a fixed,
small set of ordinary Python functions that dispatch on the CLOSED
vocabulary in apps/api/app/strategies/models.py - an `if` chain over six
known operator strings and a dict keyed by the four known indicator types,
nothing more. A definition is untrusted input submitted over HTTP on a system that places
real orders through a real broker; a definition language that could execute
arbitrary expressions would put remote code execution one endpoint away
from the trading path. The only strings this module ever *interprets* are
ones already checked against those enums.

**Deliberately not backtesting-specific.** Nothing here knows about
backtests, brokers, orders, positions, cash, or fills. It answers one
question - "what does this rule evaluate to on bar `index` of this bar
series?" - which is exactly as true for a fresh live bar as it is for a
historical one, so Phase 60's live signal engine reuses these same
functions against fresh bars rather than reimplementing them.

**`None` means "cannot be evaluated here", never "false".** Every function
below propagates `None` outward instead of substituting a default. An
indicator with insufficient history, or a crossing operator on the very
first bar of the series, has no answer - and the "never pad, never guess"
posture of marketdata/indicators.py's InsufficientDataError says the caller
must be told that rather than handed a fabricated `False` that looks like a
real evaluation. Callers treat `None` as "this rule did not fire", which is
a decision they make explicitly rather than one silently made for them.

All arithmetic is Decimal end to end - the same discipline as
marketdata/indicators.py, which these functions call rather than
recomputing any indicator math a second way.

**Fails closed against a corrupted bar, not just a well-formed one
(Phase 68, D086).** `Bar.close` is Pydantic-validated as `Field(gt=0)`, so a
`None`, NaN, or non-positive close cannot reach here through the normal
constructor - but this module's own `None`-means-"cannot answer" contract
must hold on its own, not merely because an upstream model happens to
enforce it today. Adversarial fuzzing found two real gaps this phase closed:
a `None` close raised `TypeError` out of `compute_indicator_series` (a
`None` cannot be summed with a `Decimal`), and a Decimal `NaN` close raised
`decimal.InvalidOperation` out of `evaluate_rule` (unlike IEEE-754 float,
Decimal's ordering comparisons raise on NaN rather than quietly returning
`False`). Both are now caught at the point they occur and answered with
this module's existing `None`, exactly like insufficient history - a
corrupted close is exactly as unusable as a not-yet-defined indicator.
"""

from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.marketdata.indicators import (
    InsufficientDataError,
    atr,
    ema,
    rsi,
    sma,
)
from apps.api.app.strategies.models import (
    CLOSE_ONLY_INDICATORS,
    PRICE_OPERAND_CLOSE,
    IndicatorType,
    RuleOperator,
)

_INDICATOR_FUNCTIONS: dict[IndicatorType, Callable[[Any, int], Decimal]] = {
    IndicatorType.SMA: sma,
    IndicatorType.RSI: rsi,
    IndicatorType.EMA: ema,
    IndicatorType.ATR: atr,
}
"""Every member of the closed vocabulary mapped to the pure function that
computes it. Exhaustive by construction: a member missing from this dict
raises the same explicit "did not pass validate_definition()" error an
unknown type string does, rather than a bare KeyError from somewhere
deeper."""


def compute_indicator_series(bars: list[Bar], indicator: dict) -> list[Decimal | None]:
    """One indicator value per bar, same length and order as `bars`.

    `indicator` is one entry of `definition["indicators"]` - `{"id", "type",
    "period"}` - and `bars` must be oldest-first (HistoricalBarProvider's
    contract). Entry `i` is the indicator computed from `bars[0..i]`
    inclusive, i.e. using only information available at bar `i` and never
    a later bar. This is the same `sma_at` construction
    apps/api/app/backtesting/strategy.py already uses for its one hard-coded
    SMA, generalized to the four indicator types the vocabulary allows.

    Entries where the indicator is not yet defined are `None`: SMA(period)
    and EMA(period) need `period` closes, RSI(period) and ATR(period) need
    `period + 1` (they measure `period` *changes* and `period` true ranges
    respectively), so the leading entries have no answer. ATR additionally
    yields `None` for any window containing a bar without a high or a low -
    a real possibility, since both columns are nullable. None of those
    boundaries is restated here - the indicator function raises
    InsufficientDataError and this records `None` - so the two modules
    cannot drift apart on an off-by-one.

    Raises ValueError if `indicator["type"]` is outside the closed
    vocabulary. That cannot happen for a definition that passed
    `validate_definition`, which is this function's documented
    precondition; it fails loudly rather than silently returning an
    all-`None` series that would look like a data gap.
    """
    indicator_type = indicator["type"]
    period = indicator["period"]

    try:
        member = IndicatorType(indicator_type)
        compute: Callable[[Any, int], Decimal] = _INDICATOR_FUNCTIONS[member]
    except (ValueError, KeyError):
        allowed = ", ".join(m.value for m in IndicatorType)
        raise ValueError(
            f"unknown indicator type {indicator_type!r} - allowed types are: {allowed}. "
            "This definition did not pass validate_definition()."
        ) from None

    # Most indicators read the close series; ATR also needs each bar's high
    # and low, so it is handed the bars themselves. The distinction lives in
    # CLOSE_ONLY_INDICATORS (strategies/models.py) rather than in an `if`
    # on ATR specifically, so the next high/low indicator is one set entry
    # and one dict entry rather than a new branch here.
    inputs: Sequence[Any] = (
        [bar.close for bar in bars] if member in CLOSE_ONLY_INDICATORS else bars
    )

    series: list[Decimal | None] = []
    for i in range(len(bars)):
        try:
            series.append(compute(inputs[: i + 1], period))
        except InsufficientDataError:
            series.append(None)
        except (TypeError, InvalidOperation):
            # Phase 68 (D086) fuzzing: `Bar.close` is `Field(gt=0)`, so a
            # `None` or non-Decimal close cannot reach here through
            # Pydantic's own validated constructor - but this function's
            # OWN contract ("None means cannot be evaluated here") must
            # hold independently of that upstream guarantee, not merely
            # because nothing else in this codebase currently bypasses it.
            # A `None` close makes `sum(..., Decimal(0))` raise `TypeError`
            # (arithmetic against `None`); a NaN close's own arithmetic
            # never raises here (Decimal NaN propagates through +/-), so
            # this branch exists for the corrupted-input case specifically,
            # not the NaN one (see evaluate_rule's own InvalidOperation
            # guard for where NaN actually surfaces: comparison, not
            # computation). Recording `None` is this function's existing
            # "cannot be evaluated" answer, unchanged in kind - a corrupted
            # close is exactly as unusable as an indicator with too little
            # history.
            series.append(None)
    return series


def resolve_operand(
    operand: str | int | float,
    *,
    bar: Bar,
    indicator_series: dict[str, list[Decimal | None]],
    index: int,
) -> Decimal | None:
    """The Decimal value of one rule operand, or `None` if it has none yet.

    Three cases, exactly matching what `validation.py::_validate_operand`
    admits:

      * a JSON number -> `Decimal(str(operand))`. Via `str` on purpose:
        `Decimal(0.1)` is the binary float 0.1000000000000000055..., while
        `Decimal("0.1")` is the threshold the user actually wrote.
      * the literal `"close"` -> `bar.close`. The caller passes the bar for
        `index` (i.e. `bars[index]`); this function does not index into a
        series to find it, so a crossing operator can hand it the previous
        bar without any special-casing here.
      * any other string -> that indicator's value at `index`, which is
        itself `None` when the indicator has insufficient history there.
        The `None` propagates unchanged - substituting a zero, the last
        known value, or the raw close would each invent a number nobody
        computed.
    """
    if isinstance(operand, bool):  # pragma: no cover - rejected by validate_definition
        raise ValueError(f"operand must not be a boolean, got {operand!r}")
    if isinstance(operand, int | float):
        return Decimal(str(operand))
    if operand == PRICE_OPERAND_CLOSE:
        return bar.close
    return indicator_series[operand][index]


def evaluate_rule(
    rule: dict,
    *,
    bars: list[Bar],
    indicator_series: dict[str, list[Decimal | None]],
    index: int,
) -> bool | None:
    """Whether `rule` holds at `bars[index]`, or `None` if it cannot be
    evaluated there.

    `None` is a real, expected result - not an error - and callers must
    treat it as "this rule did not fire", never as a guessed `True`/`False`.
    It arises two ways:

      * an operand resolves to `None` (an indicator without enough history
        yet at this index), or
      * a crossing operator is asked about `index == 0`. There is no bar
        before the first bar of the series you were handed, so nothing can
        be said to have *crossed* on it. This is precisely why a caller
        must supply warmup bars ahead of the window it actually wants
        signals for - the same reasoning behind the `period`-length HOLD
        prefix apps/api/app/backtesting/strategy.py already encodes.

    The two crossing operators are NOT sugar for the instantaneous
    comparisons (see RuleOperator's docstring). `gt` asks "is left above
    right on this bar"; `crosses_above` asks "was left at-or-below right on
    the previous bar and above it on this one" - a rule that fires once, on
    the bar the relationship changed, rather than on every bar it holds.
    The crossing definitions below are the same ones
    backtesting/strategy.py has always used for close-vs-SMA, generalized
    from "the SMA" to "whatever the right-hand operand resolves to".

    Raises ValueError for an operator outside the closed vocabulary, which
    a definition that passed `validate_definition` cannot contain.
    """
    op = rule["op"]

    if op in (RuleOperator.GT.value, RuleOperator.GTE.value,
              RuleOperator.LT.value, RuleOperator.LTE.value):
        bar = bars[index]
        left = resolve_operand(
            rule["left"], bar=bar, indicator_series=indicator_series, index=index
        )
        right = resolve_operand(
            rule["right"], bar=bar, indicator_series=indicator_series, index=index
        )
        if left is None or right is None:
            return None
        try:
            if op == RuleOperator.GT.value:
                return left > right
            if op == RuleOperator.GTE.value:
                return left >= right
            if op == RuleOperator.LT.value:
                return left < right
            return left <= right
        except InvalidOperation:
            # Phase 68 (D086) fuzzing: a Decimal NaN close cannot reach this
            # function through `Bar`'s own Pydantic validation, but IF a
            # corrupted close ever did, Decimal's ordering comparisons
            # (unlike IEEE-754 float, which quietly returns False against
            # NaN) raise `InvalidOperation` rather than returning a bool.
            # Neither operand is `None` here - both resolved to a real
            # Decimal - so this is not the ordinary "insufficient history"
            # case; it is "the numbers cannot be compared", which gets the
            # SAME answer this function already gives for "cannot be
            # evaluated here": `None`, never a guessed `True`/`False`.
            return None

    if op in (RuleOperator.CROSSES_ABOVE.value, RuleOperator.CROSSES_BELOW.value):
        if index == 0:
            return None
        prev_bar, curr_bar = bars[index - 1], bars[index]
        prev_left = resolve_operand(
            rule["left"], bar=prev_bar, indicator_series=indicator_series, index=index - 1
        )
        prev_right = resolve_operand(
            rule["right"], bar=prev_bar, indicator_series=indicator_series, index=index - 1
        )
        curr_left = resolve_operand(
            rule["left"], bar=curr_bar, indicator_series=indicator_series, index=index
        )
        curr_right = resolve_operand(
            rule["right"], bar=curr_bar, indicator_series=indicator_series, index=index
        )
        if prev_left is None or prev_right is None or curr_left is None or curr_right is None:
            return None
        try:
            if op == RuleOperator.CROSSES_ABOVE.value:
                return prev_left <= prev_right and curr_left > curr_right
            return prev_left >= prev_right and curr_left < curr_right
        except InvalidOperation:
            # Same guard, and the same reasoning, as the instantaneous
            # branch above: a NaN operand makes Decimal ordering
            # comparisons raise rather than answer, and "cannot be
            # compared" gets this function's existing `None`, never a
            # guessed direction of crossing.
            return None

    allowed = ", ".join(member.value for member in RuleOperator)
    raise ValueError(
        f"unknown rule operator {op!r} - allowed operators are: {allowed}. "
        "This definition did not pass validate_definition()."
    )
