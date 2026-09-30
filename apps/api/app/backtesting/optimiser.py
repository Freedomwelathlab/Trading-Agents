"""Per-setup parameter optimisation with walk-forward validation
(Phase 99, D118).

The operator asked for "an optimisation loop for each strategy: restructure
the parameters, back test and analyse for the best results ... use trailing
take profit if the trend keeps continuing". This is that loop, built so it
cannot fool itself.

**What is searched.** The exit plan (`BracketPlan`: stop buffer, the two
take-profit levels, whether the stop moves to breakeven, and the ATR
trailing stop that lets a runner keep going while a trend continues) and
the entry filter (minimum score, which directions). The detectors
themselves are not re-parameterised here - a detector with thirty knobs
fitted to 40 sessions is a description of those 40 sessions.

**How.** Coordinate descent: start from the defaults, try every value of
one parameter while holding the rest, keep the best, move to the next
parameter, repeat for `passes` rounds. About 40 backtests per window
instead of the several hundred an exhaustive grid needs, and in practice
it lands on the same region.

**Selection is on the train window only; the score is the next, unseen
window.** A parameter set is chosen on sessions [t, t+train) and scored on
[t+train, t+train+test), then the window rolls. The pooled out-of-sample
result is the only number that says whether optimising helps, and it is
reported next to the SAME walk-forward run with default parameters, so
the improvement (or lack of one) is measured, not assumed.

**A set needs `min_trades` train trades to be selectable.** Expectancy on
four trades is noise, and a search over dozens of sets will always find
one lucky four-trade set.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from apps.api.app.backtesting.brackets import BracketPlan
from apps.api.app.marketdata.structure import Direction

BOTH = (Direction.LONG, Direction.SHORT)
DIRECTION_CHOICES: dict[str, tuple[Direction, ...]] = {
    "both": BOTH,
    "long": (Direction.LONG,),
    "short": (Direction.SHORT,),
}


@dataclass(frozen=True)
class Candidate:
    """One point in the search space, as plain values so it prints and
    serialises without the dataclasses it configures."""

    atr_stop_buffer: Decimal = Decimal("0.30")
    tp1_r_multiple: Decimal = Decimal("1")
    tp2_r_multiple: Decimal = Decimal("2")
    trail_atr_multiple: Decimal | None = Decimal("1.5")
    breakeven_after_tp1: bool = True
    min_score: int = 0
    directions: str = "both"

    def plan(self, base: BracketPlan | None = None) -> BracketPlan:
        return replace(
            base or BracketPlan(),
            atr_stop_buffer=self.atr_stop_buffer,
            tp1_r_multiple=self.tp1_r_multiple,
            tp2_r_multiple=self.tp2_r_multiple,
            trail_atr_multiple=self.trail_atr_multiple,
            breakeven_after_tp1=self.breakeven_after_tp1,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "atr_stop_buffer": str(self.atr_stop_buffer),
            "tp1_r_multiple": str(self.tp1_r_multiple),
            "tp2_r_multiple": str(self.tp2_r_multiple),
            "trail_atr_multiple": (
                None if self.trail_atr_multiple is None else str(self.trail_atr_multiple)
            ),
            "breakeven_after_tp1": self.breakeven_after_tp1,
            "min_score": self.min_score,
            "directions": self.directions,
        }


SEARCH_SPACE: dict[str, Sequence[Any]] = {
    # Connected to the detectors in D125 (it was silently ignored before),
    # so it is searched again. The playbook's starting range is 0.20-0.40.
    "atr_stop_buffer": (Decimal("0.20"), Decimal("0.30"), Decimal("0.40")),
    "tp1_r_multiple": (Decimal("0.75"), Decimal("1"), Decimal("1.5")),
    "tp2_r_multiple": (Decimal("1.5"), Decimal("2"), Decimal("3")),
    "trail_atr_multiple": (Decimal("1.0"), Decimal("1.5"), Decimal("2.5"), None),
    "breakeven_after_tp1": (True, False),
    "min_score": (0, 2, 3, 4, 5),
    "directions": ("both", "long", "short"),
}
"""`trail_atr_multiple=None` means no trail (the runner rides to the time
stop); 2.5 ATR is the loose trail that lets a strong trend run. The TP
levels bound the search to realistic intraday targets on a 3x ETF."""


Runner = Callable[[Candidate, Any], list[float]]
"""(candidate, window) -> per-trade R multiples, costs included."""


@dataclass(frozen=True)
class Score:
    trades: int
    expectancy: float | None
    t_stat: float | None
    win_rate: float | None
    total_r: float

    @staticmethod
    def of(rs: Sequence[float]) -> Score:
        n = len(rs)
        if n == 0:
            return Score(0, None, None, None, 0.0)
        mean = statistics.fmean(rs)
        t = None
        if n >= 2:
            sd = statistics.stdev(rs)
            t = mean / (sd / math.sqrt(n)) if sd > 0 else None
        return Score(n, mean, t, sum(1 for r in rs if r > 0) / n, sum(rs))


def _objective(score: Score, min_trades: int) -> float:
    if score.trades < min_trades or score.expectancy is None:
        return -math.inf
    return score.expectancy


def coordinate_descent(
    run: Runner,
    window: Any,
    *,
    start: Candidate | None = None,
    space: dict[str, Sequence[Any]] | None = None,
    passes: int = 2,
    min_trades: int = 10,
) -> tuple[Candidate, Score, int]:
    """Best candidate on `window`, its score, and how many backtests ran.

    Every result is memoised by candidate, so revisiting a point in the
    second pass costs nothing."""
    space = space or SEARCH_SPACE
    cache: dict[Candidate, Score] = {}

    def evaluate(c: Candidate) -> Score:
        if c not in cache:
            cache[c] = Score.of(run(c, window))
        return cache[c]

    best = start or Candidate()
    best_score = evaluate(best)
    for _ in range(passes):
        improved = False
        for name, values in space.items():
            for value in values:
                trial = replace(best, **{name: value})
                s = evaluate(trial)
                if _objective(s, min_trades) > _objective(best_score, min_trades):
                    best, best_score, improved = trial, s, True
        if not improved:
            break
    return best, best_score, len(cache)


@dataclass
class Fold:
    train_label: str
    test_label: str
    chosen: Candidate | None
    train: Score
    test: Score
    baseline_test: Score
    backtests: int


@dataclass
class WalkForwardResult:
    setup: str
    folds: list[Fold] = field(default_factory=list)
    pooled: list[float] = field(default_factory=list)
    pooled_baseline: list[float] = field(default_factory=list)

    @property
    def oos(self) -> Score:
        return Score.of(self.pooled)

    @property
    def baseline(self) -> Score:
        return Score.of(self.pooled_baseline)


def walk_forward(
    setup: str,
    run: Runner,
    windows: Sequence[tuple[str, Any, str, Any]],
    *,
    passes: int = 2,
    min_trades: int = 10,
    space: dict[str, Sequence[Any]] | None = None,
) -> WalkForwardResult:
    """`windows` = [(train_label, train_window, test_label, test_window), ...]
    in time order. Optimises on each train window and scores the chosen set
    - and the defaults, as the baseline - on the test window that follows."""
    result = WalkForwardResult(setup=setup)
    default = Candidate()
    for train_label, train_w, test_label, test_w in windows:
        chosen, train_score, n = coordinate_descent(
            run, train_w, passes=passes, min_trades=min_trades, space=space
        )
        baseline_rs = run(default, test_w)
        result.pooled_baseline.extend(baseline_rs)
        if _objective(train_score, min_trades) == -math.inf:
            result.folds.append(
                Fold(train_label, test_label, None, train_score, Score.of([]),
                     Score.of(baseline_rs), n)
            )
            continue
        test_rs = run(chosen, test_w)
        result.pooled.extend(test_rs)
        result.folds.append(
            Fold(train_label, test_label, chosen, train_score, Score.of(test_rs),
                 Score.of(baseline_rs), n)
        )
    return result
