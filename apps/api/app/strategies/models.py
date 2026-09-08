"""The closed vocabulary a StrategyDefinition may be written in (Phase 54).

Every set below is CLOSED on purpose. A strategy definition is stored as
JSONB (`strategy_versions.definition`), which by itself would accept any
shape at all; these enums are what turn that column back into a bounded
language, and apps/api/app/strategies/validation.py is what enforces them.
Anything not listed here is an itemized validation error, never a value
that is quietly passed through to a later phase to deal with.

`IndicatorType` matches exactly what
apps/api/app/marketdata/indicators.py already implements - `sma` and `rsi`,
and nothing else. Listing an indicator type here that no deterministic
function computes would let a user save and validate a strategy this system
cannot actually run, which is a worse failure than refusing it up front:
the refusal happens while they are editing, the other one happens when a
backtest silently produces nothing.

The definition shape these describe:

    {
      "indicators": [{"id": "sma_20", "type": "sma", "period": 20}],
      "entry_rule": {"op": "crosses_above", "left": "sma_20", "right": "close"},
      "exit_rule":  {"op": "crosses_below", "left": "sma_20", "right": "close"},
      "position_sizing": {"type": "all_in"}
    }
"""

import enum


class IndicatorType(str, enum.Enum):  # noqa: UP042 (str mixin kept for interop)
    """Every indicator a strategy may declare. One-to-one with the pure
    functions in apps/api/app/marketdata/indicators.py - extend both
    together or neither."""

    SMA = "sma"
    RSI = "rsi"


class RuleOperator(str, enum.Enum):  # noqa: UP042 (str mixin kept for interop)
    """Every comparison an entry/exit rule may use.

    The four instantaneous comparisons (`gt`/`gte`/`lt`/`lte`) hold or do
    not hold on a single bar. The two crossing operators are deliberately
    NOT sugar for them: `crosses_above` means "did not hold on the previous
    bar and holds on this one", which is a different question and the one
    a signal rule usually means. Both kinds are validated identically here
    - the distinction is Phase 55's evaluator's problem, not this phase's.
    """

    CROSSES_ABOVE = "crosses_above"
    CROSSES_BELOW = "crosses_below"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"


class PositionSizingType(str, enum.Enum):  # noqa: UP042 (str mixin kept for interop)
    """How much to trade when a rule fires. Each type carries its own
    required parameter set - see REQUIRED_SIZING_KEYS below - so
    `fixed_fraction` without a `fraction` is an error rather than a silent
    default nobody chose."""

    ALL_IN = "all_in"
    FIXED_FRACTION = "fixed_fraction"
    FIXED_NOTIONAL = "fixed_notional"


PRICE_OPERAND_CLOSE = "close"
"""The one bar field a rule may name directly. Deliberately not
open/high/low: those columns exist on `market_data_bars` but no rule
vocabulary consumes them yet, and a rule referencing a field that is
nullable for some vendors would be a rule that silently stops evaluating.
Adding them later is an addition here plus a case in Phase 55's evaluator."""

DEFINITION_KEYS: tuple[str, ...] = (
    "indicators",
    "entry_rule",
    "exit_rule",
    "position_sizing",
)
"""The complete, required top-level key set. All four must be present and
no others are allowed - an unknown top-level key is rejected rather than
ignored, because the usual cause is a typo ("entry_rules") that would
otherwise leave the strategy validating cleanly with no entry rule at
all."""

RULE_KEYS: tuple[str, ...] = ("op", "left", "right")
"""The complete, required key set for `entry_rule` and `exit_rule`."""

INDICATOR_KEYS: tuple[str, ...] = ("id", "type", "period")
"""The complete, required key set for one entry of `indicators`."""

REQUIRED_SIZING_KEYS: dict[PositionSizingType, tuple[str, ...]] = {
    PositionSizingType.ALL_IN: (),
    PositionSizingType.FIXED_FRACTION: ("fraction",),
    PositionSizingType.FIXED_NOTIONAL: ("amount",),
}
"""Parameters each sizing type requires, in addition to `type` itself.
`all_in` takes none - it is fully specified by its name."""
