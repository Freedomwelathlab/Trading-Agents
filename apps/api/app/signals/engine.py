"""Evaluate a validated StrategyDefinition against the LATEST bars held for
one symbol, and say - in numbers - why the answer is what it is (Phase 61).

**Nothing here decides anything a backtest would decide differently.** The
headline signal is `backtesting/executor.py::generate_signals(bars,
definition)[-1]`, unchanged and untouched: the last element of the exact
series a backtest over these same bars would produce, already carrying the
exit-before-entry precedence that module owns. The per-rule breakdown comes
from `strategies/expressions.py`, also unchanged - the module whose own
docstring anticipated exactly this reuse ("Phase 60's live signal engine
reuses these same functions against fresh bars rather than reimplementing
them"). This file adds one thing neither of them has: a HUMAN-READABLE
account of the verdict, which spec section 25 requires before any BUY may be
shown to anybody.

**On demand, not on a schedule.** `evaluate_current_signal` is a pure
function of the bars it is handed. It opens no session, reads no clock, and
starts no job. Phase 63's deployed-strategy runner will call it once per
cycle; this phase's route calls it once per request. The plan's remark about
"the first component needing real recurring job scheduling" is about that
runner, not about this.

**Insufficient data is an answer, never an exception.** A symbol with no
ingested bars, or with fewer than its indicators need, produces a real
`CurrentSignal` saying HOLD with `insufficient_data=True` and an explanation
naming how many bars there actually are. That is the same "never pad, never
guess" posture as `marketdata/indicators.py`'s InsufficientDataError and
`expressions.py`'s `None`-means-cannot-evaluate rule - the caller is told
the truth rather than handed a fabricated HOLD indistinguishable from a
real one.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from apps.api.app.backtesting.executor import generate_signals
from apps.api.app.backtesting.strategy import Signal
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.strategies.expressions import (
    compute_indicator_series,
    evaluate_rule,
    resolve_operand,
)
from apps.api.app.strategies.models import PRICE_OPERAND_CLOSE, RuleOperator

_OPERATOR_PHRASES: dict[str, tuple[str, str]] = {
    RuleOperator.GT.value: ("is above", "is not above"),
    RuleOperator.GTE.value: ("is at or above", "is not at or above"),
    RuleOperator.LT.value: ("is below", "is not below"),
    RuleOperator.LTE.value: ("is at or below", "is not at or below"),
    RuleOperator.CROSSES_ABOVE.value: ("crossed above", "did not cross above"),
    RuleOperator.CROSSES_BELOW.value: ("crossed below", "did not cross below"),
}
"""(holds, does-not-hold) English for every operator in the closed vocabulary
of `strategies/models.py::RuleOperator`.

Both halves are spelled out rather than one being derived by prefixing "not"
to the other, because the negations that matter here are not mechanical: the
opposite of "crossed above" is "did not cross above" - which is true both
when the left operand is below the right AND when it was already above and
merely stayed there. That distinction is the whole point of the crossing
operators (see `evaluate_rule`'s docstring), and an explanation that blurred
it would misreport a HOLD as if the market were on the other side of the
line.
"""


@dataclass(frozen=True)
class CurrentSignal:
    """What a strategy says to do right now for one symbol, plus everything
    needed to check that answer by hand.

    Frozen, and carrying no ids, no symbol and no session: it is the pure
    result of evaluating rules against bars. The route is what attaches it to
    a symbol, a version and a user, and what persists it.
    """

    signal: Signal
    """BUY / SELL / HOLD - always `generate_signals(bars, definition)[-1]`,
    so this can never disagree with a backtest over the same bars."""

    as_of: datetime | None
    """The latest bar's own timestamp - the vendor's, never a fabricated
    "now". `None` only when there were zero bars to evaluate, which is why it
    is nullable rather than defaulting to the request time: "as of no data"
    and "as of this moment" are different statements."""

    latest_close: Decimal | None
    """The close the verdict was computed from. `None` only in the zero-bar
    case, for the same reason."""

    entry_rule_held: bool | None
    """`evaluate_rule(entry_rule)` at the latest bar index. `None` means the
    rule COULD NOT be evaluated there (an operand with insufficient history, or
    a crossing operator with no previous bar) - deliberately not collapsed to
    `False`, because "the entry condition is absent" and "we could not tell"
    are different facts about a strategy someone may be about to act on."""

    exit_rule_held: bool | None
    """The same, for `exit_rule`."""

    insufficient_data: bool
    """True when the verdict was NOT fully determined by rules that could
    actually be evaluated: no bars at all, or the exit rule unevaluable, or the
    exit rule absent while the entry rule was unevaluable.

    Note what is deliberately NOT flagged: an exit rule that HOLDS settles the
    answer on its own (exit beats entry - `generate_signals`' precedence), so a
    SELL is fully determined even when the entry rule had too little history.
    Flagging that as insufficient would grey out a perfectly determined exit
    signal in the one direction where being wrong leaves an unmanaged
    position.
    """

    indicator_values: dict[str, Decimal | None]
    """Every declared indicator's value AT THE LATEST BAR, `None` where it is
    not yet defined. This is what makes the row reproducible: the numbers the
    rules were actually compared against, kept beside the verdict rather than
    recoverable only by re-running the evaluator."""

    explanation: str
    """One or two sentences naming the concrete numbers behind the verdict -
    the "never a black-box BUY" contract of spec section 25. Always populated,
    including for HOLD and for insufficient data, because "why did nothing
    happen" is exactly as much a question as "why did this fire"."""


def _render_operand(
    operand: str | int | float,
    *,
    bar: Bar,
    indicator_series: dict[str, list[Decimal | None]],
    index: int,
) -> str:
    """One operand as it should read inside an explanation.

    A literal renders as just the number (`70`), because `70 (70)` says
    nothing twice. `close` and any indicator id render as `name (value)` -
    the name so the reader knows which rule they are looking at, the value so
    they can check it. An indicator with no value yet renders as
    `sma_20 (not yet defined)` rather than being omitted or shown as 0.
    """
    if not isinstance(operand, bool) and isinstance(operand, int | float):
        return str(Decimal(str(operand)))
    value = resolve_operand(operand, bar=bar, indicator_series=indicator_series, index=index)
    label = PRICE_OPERAND_CLOSE if operand == PRICE_OPERAND_CLOSE else str(operand)
    return f"{label} ({value if value is not None else 'not yet defined'})"


def _describe_rule(
    rule: dict,
    *,
    bars: list[Bar],
    indicator_series: dict[str, list[Decimal | None]],
    index: int,
    held: bool,
) -> str:
    """`"close (108.00) crossed above sma_20 (105.50)"` - one rule, at the
    latest bar, with both sides' real values, phrased for whether it held.

    Both operands are rendered at `index` (the latest bar) even for a crossing
    operator, which also consults `index - 1`: the two numbers a reader wants
    are the ones as of now, and the phrase "crossed" is what carries the
    comparison against the previous bar. `held` must be the actual result of
    `evaluate_rule` for this rule - it is never re-derived here, so the
    sentence cannot contradict the verdict it explains.
    """
    affirmative, negative = _OPERATOR_PHRASES[rule["op"]]
    bar = bars[index]
    left = _render_operand(rule["left"], bar=bar, indicator_series=indicator_series, index=index)
    right = _render_operand(rule["right"], bar=bar, indicator_series=indicator_series, index=index)
    return f"{left} {affirmative if held else negative} {right}"


def _bar_count(count: int) -> str:
    return f"{count} bar" if count == 1 else f"{count} bars"


def _insufficient_explanation(
    *,
    bar_count: int,
    day: str,
    indicator_values: dict[str, Decimal | None],
    periods: dict[str, int],
) -> str:
    """Why the strategy could not be evaluated, in the most specific terms the
    data supports.

    Preference order is deliberate. An indicator with no value at the latest
    bar is the usual cause and the most actionable one, so it is named first,
    with its period and the real bar count - "needs MORE THAN the 12 bars
    ingested" rather than a restated warmup formula, because the exact
    boundary (SMA needs `period`, RSI needs `period + 1`) lives in
    `marketdata/indicators.py` and duplicating it here is precisely the
    off-by-one drift `compute_indicator_series` refuses to risk. Failing that,
    a single bar cannot support any crossing comparison. Failing both, the
    generic sentence still carries the two concrete numbers - the bar count
    and the bar's date - that a reader needs to check the claim.
    """
    undefined = [name for name, value in indicator_values.items() if value is None]
    if undefined:
        named = ", ".join(f"{name} (period {periods[name]})" for name in undefined)
        has, it_needs = ("has", "it needs") if len(undefined) == 1 else ("have", "they need")
        return (
            f"{named} {has} no value at the latest bar ({day}): {it_needs} more than "
            f"the {_bar_count(bar_count)} ingested for this symbol "
            f"-> HOLD (insufficient data)"
        )
    if bar_count == 1:
        return (
            "only 1 bar is ingested for this symbol, so there is no previous bar to "
            "measure a crossing against -> HOLD (insufficient data)"
        )
    return (
        f"the strategy's rules could not be evaluated at the latest bar ({day}) from "
        f"the {_bar_count(bar_count)} ingested for this symbol "
        f"-> HOLD (insufficient data)"
    )


def evaluate_current_signal(bars: list[Bar], definition: dict) -> CurrentSignal:
    """What `definition` says to do at the LAST of `bars`, and why.

    `bars` must be oldest-first (HistoricalBarProvider's contract) and
    `definition` must already satisfy `validate_definition(definition) == []`
    - i.e. it came off a VALIDATED `StrategyVersion`. This function does not
    re-validate, trusting its caller's contract exactly as
    `generate_signals` does.

    Never raises for insufficient data. Zero bars, or too few for the
    indicators to be defined at the latest bar, produce a real result with
    `signal=HOLD`, `insufficient_data=True` and an explanation naming how many
    bars were actually available. It DOES still raise `ValueError` for a
    definition outside the closed vocabulary, which the already-validated
    precondition makes unreachable - failing loudly beats explaining a
    definition it did not understand.
    """
    indicators = definition.get("indicators") or []

    if not bars:
        return CurrentSignal(
            signal=Signal.HOLD,
            as_of=None,
            latest_close=None,
            entry_rule_held=None,
            exit_rule_held=None,
            insufficient_data=True,
            indicator_values={},
            explanation=("no bars are ingested for this symbol -> HOLD (insufficient data)"),
        )

    indicator_series: dict[str, list[Decimal | None]] = {
        indicator["id"]: compute_indicator_series(bars, indicator) for indicator in indicators
    }
    index = len(bars) - 1
    latest = bars[index]
    day = latest.ts.date().isoformat()
    indicator_values = {name: series[index] for name, series in indicator_series.items()}

    entry_rule = definition["entry_rule"]
    exit_rule = definition["exit_rule"]
    entry_held = evaluate_rule(
        entry_rule, bars=bars, indicator_series=indicator_series, index=index
    )
    exit_held = evaluate_rule(exit_rule, bars=bars, indicator_series=indicator_series, index=index)

    # The headline verdict is the executor's, not a second opinion computed
    # here - the last element of the exact signal series a backtest over these
    # same bars would produce.
    signal = generate_signals(bars, definition)[-1]

    determined = exit_held is True or (exit_held is False and entry_held is not None)

    if signal is Signal.SELL:
        exit_text = _describe_rule(
            exit_rule, bars=bars, indicator_series=indicator_series, index=index, held=True
        )
        if entry_held:
            explanation = (
                f"exit rule holds: {exit_text} on the latest bar ({day}); the exit rule "
                f"takes precedence over the entry rule, which also holds -> SELL"
            )
        else:
            explanation = f"exit rule holds: {exit_text} on the latest bar ({day}) -> SELL"
    elif signal is Signal.BUY:
        entry_text = _describe_rule(
            entry_rule, bars=bars, indicator_series=indicator_series, index=index, held=True
        )
        caveat = (
            ""
            if exit_held is not None
            else " - note the exit rule could not be evaluated at this bar for want of data"
        )
        explanation = f"{entry_text} on the latest bar ({day}); entry rule holds -> BUY{caveat}"
    elif determined:
        entry_text = _describe_rule(
            entry_rule, bars=bars, indicator_series=indicator_series, index=index, held=False
        )
        exit_text = _describe_rule(
            exit_rule, bars=bars, indicator_series=indicator_series, index=index, held=False
        )
        explanation = (
            f"on the latest bar ({day}) {entry_text}, and {exit_text}; neither rule fired -> HOLD"
        )
    else:
        explanation = _insufficient_explanation(
            bar_count=len(bars),
            day=day,
            indicator_values=indicator_values,
            periods={indicator["id"]: indicator["period"] for indicator in indicators},
        )

    return CurrentSignal(
        signal=signal,
        as_of=latest.ts,
        latest_close=latest.close,
        entry_rule_held=entry_held,
        exit_rule_held=exit_held,
        insufficient_data=not determined,
        indicator_values=indicator_values,
        explanation=explanation,
    )
