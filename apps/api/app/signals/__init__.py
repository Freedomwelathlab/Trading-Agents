"""The signal engine (Phase 61): what a validated strategy says to do RIGHT
NOW, off the latest bars this system actually holds, and exactly why.

A backtest asks "how would this definition have done over that window"; this
package asks the present-tense question the spec's sections 25 and 52 are
about - "does this strategy currently say BUY, SELL or HOLD for this symbol,
and what concrete numbers make it say so". Nothing here re-implements the
answer: `apps/api/app/backtesting/executor.py::generate_signals` produces the
headline signal and `apps/api/app/strategies/expressions.py` produces the
per-rule breakdown, so a current signal can never disagree with the backtest
that would have been run over the same bars.

**On demand this phase, not scheduled.** A request evaluates and persists;
there is no recurring job here. The plan's line about this being "the first
component needing real recurring job scheduling" describes Phase 63's
deployed-strategy runner, which will call `evaluate_current_signal` once per
cycle - this phase builds the thing that runner will call, not the runner.

**Never a black-box BUY.** `CurrentSignal.explanation` always names the real
numbers behind the verdict, and `indicator_values` carries every declared
indicator's value at the evaluated bar, so a persisted signal is reproducible
from its own row rather than only from a re-run.
"""
