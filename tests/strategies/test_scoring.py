"""Unit tests for apps/api/app/strategies/scoring.py - the scoring model
itself, with no database involved.

The rows here are REAL ORM instances (`BacktestRun(...)`, `WalkForwardRun(...)`
and friends) that are simply never added to a session. That is deliberate
over a hand-rolled stub: the model reads attributes off these classes, so
constructing the actual classes is what makes a renamed or retyped column
break these tests instead of sailing past a stub that agreed with the old
shape. The DB-backed half - four phases' worth of really-persisted runs,
scored through the real route - is tests/api/test_leaderboard.py's.

Everything below calls `build_strategy_score`, the pure function
`compute_strategy_score` delegates to once it has selected the latest
succeeded run of each type. The selection half needs a session and is
therefore covered in the integration module; the arithmetic and the status
heuristic are here, where they can be pinned exactly.

Every asserted point value is hand-derived from the documented scale rather
than copied from a run of the code, so a change to a scale fails these tests
rather than being silently absorbed.
"""

import uuid
from decimal import Decimal

from apps.api.app.db.models import (
    BacktestRun,
    MonteCarloRun,
    RobustnessRun,
    WalkForwardRun,
)
from apps.api.app.strategies.scoring import (
    MAX_COMPONENT_POINTS,
    STATUS_INSUFFICIENT_DATA,
    STATUS_OVERFIT_RISK,
    STATUS_PROMISING,
    STATUS_VALIDATED,
    build_strategy_score,
    satisfies_min_status,
)

VERSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _backtest(
    total_return_pct: str | None = "10", max_drawdown_pct: str | None = "15"
) -> BacktestRun:
    """A succeeded backtest carrying the two metrics the model reads.

    `10` / `15` are the neutral defaults every test that is not about the
    return or risk scales uses, so those tests' totals stay easy to check by
    hand: 10% return is half of the 20% full-points mark (12.5 points), and a
    15% drawdown is halfway to the 30% zero mark (12.5 points).
    """
    return BacktestRun(
        id=uuid.uuid4(),
        total_return_pct=None if total_return_pct is None else Decimal(total_return_pct),
        max_drawdown_pct=None if max_drawdown_pct is None else Decimal(max_drawdown_pct),
    )


def _walk_forward(profitable: int | None, windows: int | None) -> WalkForwardRun:
    return WalkForwardRun(
        id=uuid.uuid4(),
        num_windows=windows,
        num_succeeded_windows=windows,
        num_profitable_windows=profitable,
    )


def _robustness(max_return_deviation_pct: str | None) -> RobustnessRun:
    return RobustnessRun(
        id=uuid.uuid4(),
        max_return_deviation_pct=(
            None
            if max_return_deviation_pct is None
            else Decimal(max_return_deviation_pct)
        ),
    )


def _component(score, name: str):
    matches = [item for item in score.components if item.name == name]
    assert len(matches) == 1, f"expected exactly one {name!r} component, got {matches}"
    return matches[0]


def _names(score) -> set[str]:
    return {item.name for item in score.components}


# --------------------------------------------------------------------------
# No backtest at all
# --------------------------------------------------------------------------


def test_no_backtest_at_all_is_none_not_a_zero_score() -> None:
    """"Nobody has backtested this" is not a result of zero - there is
    nothing to measure, so there is no score, and the leaderboard excludes
    the strategy rather than ranking it below strategies that really did
    badly."""
    assert build_strategy_score(VERSION_ID, None) is None


def test_a_walk_forward_run_without_a_backtest_still_scores_nothing() -> None:
    """The backtest is the floor of the whole model: return and risk are what
    every other component is read alongside, so no backtest means no score
    even when other runs exist."""
    assert (
        build_strategy_score(
            VERSION_ID, None, walk_forward_run=_walk_forward(5, 5)
        )
        is None
    )


# --------------------------------------------------------------------------
# Backtest only
# --------------------------------------------------------------------------


def test_backtest_only_is_insufficient_data_with_two_components_out_of_fifty() -> None:
    score = build_strategy_score(VERSION_ID, _backtest())

    assert score is not None
    assert _names(score) == {"return", "risk"}
    assert score.components_measured == 2
    # 25 out of a possible 50 - never out of 100. The two components nobody
    # ran are absent, not zero.
    assert score.max_possible_points == Decimal("50.0000")
    assert score.total_points == Decimal("25.0000")
    assert score.percentage == Decimal("50.0000")
    assert score.status == STATUS_INSUFFICIENT_DATA
    assert "no walk-forward run" in score.status_reason.lower()
    assert "unmeasured" in score.status_reason


def test_provenance_ids_are_carried_and_absent_runs_are_null() -> None:
    backtest = _backtest()
    monte_carlo = MonteCarloRun(id=uuid.uuid4())

    score = build_strategy_score(VERSION_ID, backtest, monte_carlo_run=monte_carlo)

    assert score is not None
    assert score.strategy_version_id == VERSION_ID
    assert score.latest_backtest_run_id == backtest.id
    # Reported for auditability, and deliberately scores nothing this phase.
    assert score.latest_monte_carlo_run_id == monte_carlo.id
    assert "monte" not in " ".join(_names(score))
    assert score.latest_walk_forward_run_id is None
    assert score.latest_robustness_run_id is None


# --------------------------------------------------------------------------
# The return scale, including both clamp boundaries
# --------------------------------------------------------------------------


def test_a_negative_return_earns_zero_points_never_negative_ones() -> None:
    """A losing strategy scores 0 on return rather than dragging its other,
    unrelated components down with a negative number."""
    score = build_strategy_score(VERSION_ID, _backtest(total_return_pct="-12.5"))

    assert score is not None
    assert _component(score, "return").points == Decimal("0.0000")
    assert "-12.5%" in _component(score, "return").detail


def test_a_zero_return_earns_zero_points() -> None:
    score = build_strategy_score(VERSION_ID, _backtest(total_return_pct="0"))
    assert score is not None
    assert _component(score, "return").points == Decimal("0.0000")


def test_a_ten_percent_return_earns_half_the_component() -> None:
    score = build_strategy_score(VERSION_ID, _backtest(total_return_pct="10"))
    assert score is not None
    assert _component(score, "return").points == Decimal("12.5000")


def test_exactly_twenty_percent_return_earns_the_full_component() -> None:
    score = build_strategy_score(VERSION_ID, _backtest(total_return_pct="20"))
    assert score is not None
    assert _component(score, "return").points == MAX_COMPONENT_POINTS


def test_a_twenty_five_percent_return_is_capped_at_the_full_component() -> None:
    """Past 20% the component is capped, so an outsized return cannot buy
    its way past the three dimensions that ask whether the result is real -
    the concrete mechanism behind never optimizing solely for ROI."""
    score = build_strategy_score(VERSION_ID, _backtest(total_return_pct="25"))
    assert score is not None
    assert _component(score, "return").points == MAX_COMPONENT_POINTS

    huge = build_strategy_score(VERSION_ID, _backtest(total_return_pct="400"))
    assert huge is not None
    assert _component(huge, "return").points == MAX_COMPONENT_POINTS


def test_the_return_detail_names_the_column_and_the_scale() -> None:
    """Never a black box: the sentence a reader sees has to name the real
    column value AND the scale that turned it into points."""
    score = build_strategy_score(VERSION_ID, _backtest(total_return_pct="12.4500"))
    assert score is not None
    detail = _component(score, "return").detail
    assert "total_return_pct=12.4500%" in detail
    assert "20%+" in detail
    assert "25pts" in detail


# --------------------------------------------------------------------------
# The risk scale, including both clamp boundaries
# --------------------------------------------------------------------------


def test_a_zero_drawdown_earns_the_full_risk_component() -> None:
    score = build_strategy_score(VERSION_ID, _backtest(max_drawdown_pct="0"))
    assert score is not None
    assert _component(score, "risk").points == MAX_COMPONENT_POINTS


def test_a_fifteen_percent_drawdown_earns_half_the_risk_component() -> None:
    score = build_strategy_score(VERSION_ID, _backtest(max_drawdown_pct="15"))
    assert score is not None
    assert _component(score, "risk").points == Decimal("12.5000")


def test_exactly_thirty_percent_drawdown_earns_zero() -> None:
    score = build_strategy_score(VERSION_ID, _backtest(max_drawdown_pct="30"))
    assert score is not None
    assert _component(score, "risk").points == Decimal("0.0000")


def test_a_forty_percent_drawdown_is_clamped_to_zero_not_negative() -> None:
    score = build_strategy_score(VERSION_ID, _backtest(max_drawdown_pct="40"))
    assert score is not None
    assert _component(score, "risk").points == Decimal("0.0000")
    assert "max_drawdown_pct=40%" in _component(score, "risk").detail


# --------------------------------------------------------------------------
# Consistency (walk-forward)
# --------------------------------------------------------------------------


def test_backtest_plus_a_healthy_walk_forward_is_promising() -> None:
    """One of the two checks has been run and it did not raise a flag - that
    is `promising`, not `validated`: the parameter-stability question has not
    been asked at all yet, and the reason string says so."""
    score = build_strategy_score(
        VERSION_ID, _backtest(), walk_forward_run=_walk_forward(5, 6)
    )

    assert score is not None
    assert score.status == STATUS_PROMISING
    assert _names(score) == {"return", "risk", "consistency"}
    assert score.components_measured == 3
    assert score.max_possible_points == Decimal("75.0000")
    # 5/6 of 25 points.
    assert _component(score, "consistency").points == Decimal("20.8333")
    assert "5/6 profitable windows" in score.status_reason
    assert "no robustness run" in score.status_reason


def test_backtest_plus_a_failing_walk_forward_is_overfit_risk() -> None:
    """Fewer than half the out-of-sample windows profitable is the flag, and
    the reason names the actual counts rather than announcing a bare
    label."""
    score = build_strategy_score(
        VERSION_ID, _backtest(), walk_forward_run=_walk_forward(2, 6)
    )

    assert score is not None
    assert score.status == STATUS_OVERFIT_RISK
    assert "2/6 profitable windows" in score.status_reason
    assert "33%" in score.status_reason
    # Still a real, scored component - flagged is not the same as unmeasured.
    assert _component(score, "consistency").points == Decimal("8.3333")


def test_exactly_half_the_windows_profitable_is_not_flagged() -> None:
    """The threshold is `< 0.5`, so a 50% ratio passes. Pinned because an
    off-by-one on a boundary is exactly the kind of change that would
    silently relabel real strategies."""
    score = build_strategy_score(
        VERSION_ID, _backtest(), walk_forward_run=_walk_forward(3, 6)
    )
    assert score is not None
    assert score.status == STATUS_PROMISING


def test_a_walk_forward_run_with_no_counts_is_absent_not_zero() -> None:
    """A SUCCEEDED walk-forward run can still carry NULL aggregates. That is
    a run that measured nothing, so consistency is ABSENT - scoring it 0
    would state that the strategy failed a test nobody actually ran."""
    score = build_strategy_score(
        VERSION_ID, _backtest(), walk_forward_run=_walk_forward(None, None)
    )

    assert score is not None
    assert _names(score) == {"return", "risk"}
    assert score.status == STATUS_INSUFFICIENT_DATA
    assert score.max_possible_points == Decimal("50.0000")
    # The run is still reported, so a reader can go and look at it.
    assert score.latest_walk_forward_run_id is not None


def test_a_walk_forward_run_with_zero_windows_never_divides_by_zero() -> None:
    score = build_strategy_score(
        VERSION_ID, _backtest(), walk_forward_run=_walk_forward(0, 0)
    )

    assert score is not None
    assert _names(score) == {"return", "risk"}
    assert score.status == STATUS_INSUFFICIENT_DATA


# --------------------------------------------------------------------------
# Parameter stability (robustness)
# --------------------------------------------------------------------------


def test_backtest_plus_a_stable_robustness_run_is_promising() -> None:
    score = build_strategy_score(
        VERSION_ID, _backtest(), robustness_run=_robustness("4")
    )

    assert score is not None
    assert score.status == STATUS_PROMISING
    assert _names(score) == {"return", "risk", "parameter_stability"}
    # (20 - 4) / 20 * 25 = 20 points.
    assert _component(score, "parameter_stability").points == Decimal("20.0000")
    assert "no walk-forward run" in score.status_reason


def test_backtest_plus_an_unstable_robustness_run_is_overfit_risk() -> None:
    """A nudge of a parameter moving the return by more than 20 percentage
    points means the reported result belonged to one exact parameter set -
    which is the definition of the risk this status names."""
    score = build_strategy_score(
        VERSION_ID, _backtest(), robustness_run=_robustness("35")
    )

    assert score is not None
    assert score.status == STATUS_OVERFIT_RISK
    assert "35" in score.status_reason
    # It scored 0 on that component AND is flagged - the two say the same
    # thing from different directions.
    assert _component(score, "parameter_stability").points == Decimal("0.0000")


def test_exactly_twenty_points_of_deviation_is_not_flagged() -> None:
    """The threshold is `> 20`, so exactly 20 passes - and scores 0 on the
    component, which is the boundary being pinned here."""
    score = build_strategy_score(
        VERSION_ID, _backtest(), robustness_run=_robustness("20")
    )
    assert score is not None
    assert score.status == STATUS_PROMISING
    assert _component(score, "parameter_stability").points == Decimal("0.0000")


def test_a_robustness_run_with_a_null_deviation_is_absent_not_zero() -> None:
    """A SUCCEEDED robustness run whose every perturbation failed carries a
    NULL deviation (see RobustnessRun's docstring). Nothing was measured, so
    the component is absent - and the run id is still reported."""
    robustness = _robustness(None)
    score = build_strategy_score(VERSION_ID, _backtest(), robustness_run=robustness)

    assert score is not None
    assert _names(score) == {"return", "risk"}
    assert score.status == STATUS_INSUFFICIENT_DATA
    assert score.latest_robustness_run_id == robustness.id


# --------------------------------------------------------------------------
# Both checks present
# --------------------------------------------------------------------------


def test_both_checks_present_and_both_good_is_validated() -> None:
    score = build_strategy_score(
        VERSION_ID,
        _backtest(total_return_pct="20", max_drawdown_pct="0"),
        walk_forward_run=_walk_forward(6, 6),
        robustness_run=_robustness("0"),
    )

    assert score is not None
    assert score.status == STATUS_VALIDATED
    assert score.components_measured == 4
    assert score.max_possible_points == Decimal("100.0000")
    assert score.total_points == Decimal("100.0000")
    assert score.percentage == Decimal("100.0000")
    assert "2 of 2 available checks passed" in score.status_reason
    assert "6/6 profitable windows" in score.status_reason


def test_both_present_but_the_walk_forward_bad_is_overfit_risk_not_validated() -> None:
    """One warning sign is enough. Overfit risk takes priority over
    `validated` even though the other check passed - and the reason names
    both halves so a reader sees which one raised the flag."""
    score = build_strategy_score(
        VERSION_ID,
        _backtest(),
        walk_forward_run=_walk_forward(1, 6),
        robustness_run=_robustness("1"),
    )

    assert score is not None
    assert score.status == STATUS_OVERFIT_RISK
    assert "1 of 2 available checks passed" in score.status_reason
    assert "1/6 profitable windows" in score.status_reason
    assert "robustness showed a 1" in score.status_reason


def test_both_present_but_the_robustness_bad_is_overfit_risk_not_validated() -> None:
    score = build_strategy_score(
        VERSION_ID,
        _backtest(),
        walk_forward_run=_walk_forward(6, 6),
        robustness_run=_robustness("45"),
    )

    assert score is not None
    assert score.status == STATUS_OVERFIT_RISK
    assert "1 of 2 available checks passed" in score.status_reason


def test_both_checks_failing_reports_both_reasons() -> None:
    score = build_strategy_score(
        VERSION_ID,
        _backtest(),
        walk_forward_run=_walk_forward(0, 4),
        robustness_run=_robustness("60"),
    )

    assert score is not None
    assert score.status == STATUS_OVERFIT_RISK
    assert "0 of 2 available checks passed" in score.status_reason
    assert "0/4 profitable windows" in score.status_reason
    assert "60" in score.status_reason


# --------------------------------------------------------------------------
# Normalization - the actual ranking key
# --------------------------------------------------------------------------


def test_percentage_is_computed_over_only_the_present_components() -> None:
    """The whole reason `percentage` and not `total_points` is the ranking
    key: 40/50 and 80/100 are the same standing, so a strategy is never
    rewarded for having skipped the tests that might have gone badly."""
    two_components = build_strategy_score(
        VERSION_ID,
        # 16% return -> 20 points; 6% drawdown -> 20 points. 40 of 50.
        _backtest(total_return_pct="16", max_drawdown_pct="6"),
    )
    four_components = build_strategy_score(
        VERSION_ID,
        _backtest(total_return_pct="16", max_drawdown_pct="6"),
        walk_forward_run=_walk_forward(4, 5),
        robustness_run=_robustness("4"),
    )

    assert two_components is not None and four_components is not None
    assert two_components.total_points == Decimal("40.0000")
    assert two_components.max_possible_points == Decimal("50.0000")
    assert two_components.percentage == Decimal("80.0000")

    # 20 + 20 + (4/5 * 25 = 20) + ((20-4)/20*25 = 20) = 80 of 100.
    assert four_components.total_points == Decimal("80.0000")
    assert four_components.max_possible_points == Decimal("100.0000")
    assert four_components.percentage == Decimal("80.0000")

    assert two_components.percentage == four_components.percentage
    assert two_components.components_measured == 2
    assert four_components.components_measured == 4


def test_every_component_reports_its_own_max_points() -> None:
    """`max_points` is a real field on every component, so a client renders
    `points/max_points` from the response instead of hardcoding 25."""
    score = build_strategy_score(
        VERSION_ID,
        _backtest(),
        walk_forward_run=_walk_forward(3, 4),
        robustness_run=_robustness("2"),
    )
    assert score is not None
    assert all(item.max_points == MAX_COMPONENT_POINTS for item in score.components)
    assert score.max_possible_points == MAX_COMPONENT_POINTS * 4


# --------------------------------------------------------------------------
# min_status tiers
# --------------------------------------------------------------------------


def test_no_min_status_admits_every_tier_including_a_flagged_one() -> None:
    for status in (
        STATUS_INSUFFICIENT_DATA,
        STATUS_PROMISING,
        STATUS_VALIDATED,
        STATUS_OVERFIT_RISK,
    ):
        assert satisfies_min_status(status, None) is True


def test_min_status_is_an_ordered_floor() -> None:
    assert satisfies_min_status(STATUS_VALIDATED, STATUS_PROMISING) is True
    assert satisfies_min_status(STATUS_PROMISING, STATUS_PROMISING) is True
    assert satisfies_min_status(STATUS_INSUFFICIENT_DATA, STATUS_PROMISING) is False
    assert satisfies_min_status(STATUS_PROMISING, STATUS_VALIDATED) is False


def test_an_overfit_flagged_strategy_never_satisfies_a_floor_above_the_bottom() -> None:
    """A flagged strategy may score numerically well on the components that
    were measured. Letting it answer a request for `min_status=promising` on
    that basis would surface exactly the strategy the flag exists to warn
    about."""
    assert satisfies_min_status(STATUS_OVERFIT_RISK, STATUS_INSUFFICIENT_DATA) is True
    assert satisfies_min_status(STATUS_OVERFIT_RISK, STATUS_PROMISING) is False
    assert satisfies_min_status(STATUS_OVERFIT_RISK, STATUS_VALIDATED) is False
