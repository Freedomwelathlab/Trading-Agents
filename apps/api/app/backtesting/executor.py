"""Signal generation for an arbitrary, user-authored StrategyDefinition
(Phase 55) - the generalization of apps/api/app/backtesting/strategy.py's
one hard-coded SMA(20) crossover.

strategy.py is untouched and stays that way (docs/DECISIONS.md D025): it is
the v1 engine's strategy and its behavior is the thing this module is
measured against, not something to be edited into agreement. Feeding this
module a definition that is the logical equivalent of D025's rule must
produce D025's exact signals bar for bar - tests/backtesting/test_executor.py
asserts precisely that, elementwise.

Everything about interpreting the definition lives in
apps/api/app/strategies/expressions.py, which is deliberately free of any
backtest concept so Phase 60's live signal engine can reuse it against
fresh bars. This module is only the loop over bars plus the precedence
decision below - and, like strategy.py, it has no notion of position, cash,
or risk limits. It proposes a Signal; the engine decides what, if anything,
to do with it, and every actual trade still goes through the real Risk
Engine.
"""

from decimal import Decimal

from apps.api.app.backtesting.strategy import Signal
from apps.api.app.marketdata.bar_provider import Bar
from apps.api.app.strategies.expressions import compute_indicator_series, evaluate_rule


def generate_signals(bars: list[Bar], definition: dict) -> list[Signal]:
    """One Signal per bar, same length and order as `bars`.

    `bars` must be oldest-first (HistoricalBarProvider's contract), and
    `definition` must already satisfy `validate_definition(definition) == []`.
    This function does not re-validate - it trusts its caller's contract the
    same way backtesting/engine.py trusts HistoryProvider's rather than
    re-checking every bar it is handed.

    **Exit wins over entry.** If `exit_rule` holds at a bar, the signal is
    SELL even when `entry_rule` also holds there; only if the exit did not
    fire does a firing `entry_rule` produce BUY. That order is deliberate,
    and it follows from this function having no notion of current position
    (the same separation of concerns strategy.py's docstring states). A
    caller that is holding a position needs to hear about an exit on a bar
    where the entry condition coincidentally also holds - suppressing the
    SELL there would silently keep it in a trade its own rules said to
    leave. A caller that is flat simply ignores the SELL, exactly as D025's
    engine already ignores a SELL it receives while flat. Losing an entry
    signal is a missed opportunity; losing an exit signal is an unmanaged
    position, so the tie breaks toward the exit.

    Everything else is HOLD - including every bar where a rule evaluated to
    `None` for want of data (an indicator still inside its warmup, or a
    crossing operator on bar 0). Insufficient data for one bar is never an
    exception here: it degrades to HOLD, the same "never pad, never guess"
    posture as the `period`-length HOLD prefix in strategy.py and as
    InsufficientDataError itself.

    Raises ValueError only for a definition that violates the
    already-validated precondition - e.g. an indicator type or rule
    operator outside the closed vocabulary in
    apps/api/app/strategies/models.py. That should never occur in practice;
    it fails loudly rather than quietly emitting signals from a definition
    it did not actually understand.
    """
    indicator_series: dict[str, list[Decimal | None]] = {
        indicator["id"]: compute_indicator_series(bars, indicator)
        for indicator in definition["indicators"]
    }

    entry_rule = definition["entry_rule"]
    exit_rule = definition["exit_rule"]

    signals: list[Signal] = []
    for index in range(len(bars)):
        exits = evaluate_rule(
            exit_rule, bars=bars, indicator_series=indicator_series, index=index
        )
        if exits:
            signals.append(Signal.SELL)
            continue
        enters = evaluate_rule(
            entry_rule, bars=bars, indicator_series=indicator_series, index=index
        )
        signals.append(Signal.BUY if enters else Signal.HOLD)

    return signals
