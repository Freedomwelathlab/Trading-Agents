"""Walk-forward parameter optimiser (Phase 99, D118), against a fake runner."""

from decimal import Decimal

from apps.api.app.backtesting.optimiser import (
    Candidate,
    Score,
    coordinate_descent,
    walk_forward,
)


def test_score_reports_none_not_zero_for_no_trades():
    s = Score.of([])
    assert s.trades == 0 and s.expectancy is None and s.win_rate is None


def test_score_basic_statistics():
    s = Score.of([1.0, -1.0, 2.0, 0.0])
    assert s.trades == 4
    assert s.expectancy == 0.5
    assert s.win_rate == 0.5
    assert s.total_r == 2.0
    assert s.t_stat is not None


def _runner_prefers(target: Candidate, n: int = 20):
    """A fake backtest: the closer a candidate is to `target`, the better,
    always with `n` trades."""

    def run(c: Candidate, _window) -> list[float]:
        distance = sum(
            1 for f in ("atr_stop_buffer", "tp1_r_multiple", "tp2_r_multiple",
                        "trail_atr_multiple", "breakeven_after_tp1", "min_score", "directions")
            if getattr(c, f) != getattr(target, f)
        )
        return [1.0 - 0.3 * distance] * n

    return run


def test_coordinate_descent_finds_the_best_point_and_memoises():
    target = Candidate(
        atr_stop_buffer=Decimal("0.40"),
        tp1_r_multiple=Decimal("1.5"),
        trail_atr_multiple=Decimal("2.5"),
        breakeven_after_tp1=False,
        min_score=3,
        directions="long",
    )
    best, score, runs = coordinate_descent(_runner_prefers(target), None, passes=2)
    assert best == target
    assert score.expectancy == 1.0
    # Far fewer backtests than the full grid (3*3*3*4*2*5*3 = 3240).
    assert runs < 60


def test_a_set_below_the_trade_minimum_is_never_selected():
    def run(c: Candidate, _w) -> list[float]:
        # The only "great" set has 3 trades; everything else is mediocre.
        if c.min_score == 5:
            return [5.0, 5.0, 5.0]
        return [0.1] * 20

    best, _, _ = coordinate_descent(run, None, min_trades=10)
    assert best.min_score != 5


def test_walk_forward_scores_on_the_test_window_and_keeps_a_baseline():
    seen: list[str] = []

    def run(c: Candidate, window) -> list[float]:
        seen.append(window)
        if window.startswith("train"):
            return [0.5 if c.directions == "long" else 0.0] * 12
        # Out of sample the "long" choice is worse than the default.
        return [-0.2] * 10 if c.directions == "long" else [0.1] * 10

    wf = walk_forward(
        "x",
        run,
        [("t1", "train-1", "s1", "test-1"), ("t2", "train-2", "s2", "test-2")],
        passes=1,
    )
    assert [f.chosen.directions for f in wf.folds] == ["long", "long"]
    assert wf.oos.expectancy is not None and abs(wf.oos.expectancy - (-0.2)) < 1e-9
    assert wf.baseline.expectancy is not None and abs(wf.baseline.expectancy - 0.1) < 1e-9
    # The test window is only ever scored, never searched.
    assert seen.count("test-1") == 2  # chosen + baseline, nothing else


def test_a_fold_with_no_selectable_set_is_reported_not_filled():
    wf = walk_forward("x", lambda c, w: [1.0] * 3, [("t", "tr", "s", "te")], passes=1)
    assert wf.folds[0].chosen is None
    assert wf.oos.trades == 0
    assert wf.baseline.trades == 3


def test_the_stop_buffer_is_searched_now_that_the_engine_uses_it():
    from apps.api.app.backtesting.optimiser import SEARCH_SPACE

    assert "atr_stop_buffer" in SEARCH_SPACE
