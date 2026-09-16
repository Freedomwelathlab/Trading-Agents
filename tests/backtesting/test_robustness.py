"""The Phase 58 robustness orchestrator's own mechanics: what it persists,
what it aggregates over, and which failures end one perturbation versus the
whole run.

**Both engine_v2 helpers are patched out in every test here, deliberately,
and there is no database.** This module orchestrates the engine; it is not
the engine's test (tests/backtesting/test_engine_v2.py is, against a real
Postgres). Injecting a hand-chosen final equity per replay instead makes
every expected number below hand-derivable: `metrics.py` is left REAL, so a
`starting_cash` of 10,000 and a final equity of 11,000 really is +10.0000%,
and the mean, the sample standard deviation and the maximum deviation are
then arithmetic anyone can check on paper. The DB-backed half - real bars,
real perturbed definitions, real persisted rows - is tests/api/test_robustness.py's job.

`generate_perturbed_definitions` is patched too, at the name
`backtesting/robustness.py` imported it under, so these tests pin the
ORCHESTRATOR's contract with the generator (empty list, several variants,
one variant) rather than whatever the real generator currently emits for a
given definition. tests/strategies/test_perturbation.py owns that half. The
`Perturbation` objects injected are the real frozen dataclass, so the shape
being relied on is the real shape.

The session is a hand-written fake for the same reason
tests/backtesting/test_walk_forward.py's is: `run_robustness_test` only ever
calls `add` / `add_all` / `flush` / `commit` / `refresh` on it, so a fake
records exactly what would have been written without needing a live
connection, and the perturbation rows it collects are asserted on directly.
"""

import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.api.app.backtesting.costs import CostModel
from apps.api.app.backtesting.errors import InsufficientHistoryError
from apps.api.app.backtesting.robustness import run_robustness_test
from apps.api.app.db.models import (
    RobustnessPerturbationResult,
    RobustnessRun,
    RobustnessRunStatus,
)
from apps.api.app.risk.models import RiskLimits
from apps.api.app.strategies.perturbation import Perturbation

START = date(2026, 1, 5)
END = date(2026, 3, 31)
STARTING_CASH = Decimal(10_000)

BASE_DEFINITION = {
    "indicators": [{"id": "sma_20", "type": "sma", "period": 20}],
    "entry_rule": {"op": "crosses_above", "left": "close", "right": "sma_20"},
    "exit_rule": {"op": "crosses_below", "left": "close", "right": "sma_20"},
    "position_sizing": {"type": "all_in"},
}


class FakeSession:
    """Records what would have been written. `flush` and `refresh` are no-ops
    because nothing here has a database to round-trip through; that
    NUMERIC(10,4) rounding really happens is the DB-backed test's assertion,
    not this module's."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def add_all(self, objs) -> None:
        self.added.extend(objs)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: object) -> None:
        return None

    @property
    def robustness_run(self) -> RobustnessRun:
        runs = [obj for obj in self.added if isinstance(obj, RobustnessRun)]
        assert len(runs) == 1
        return runs[0]

    @property
    def perturbation_rows(self) -> list[RobustnessPerturbationResult]:
        return [
            obj for obj in self.added if isinstance(obj, RobustnessPerturbationResult)
        ]


def _risk_limits() -> RiskLimits:
    """Never actually consulted - `_replay_window` is patched out - but
    passed through as the real type so the call signature is exercised
    exactly as the route builds it."""
    return RiskLimits(
        max_position_pct_of_equity=Decimal("0.30"),
        max_portfolio_exposure_pct_of_equity=Decimal(1),
        max_risk_pct_of_equity_per_trade=Decimal("0.01"),
        require_stop_price=False,
        max_market_data_age_seconds=86_400,
        duplicate_order_window_seconds=5,
    )


def _perturbation(period: int, direction: str) -> Perturbation:
    """One variant of BASE_DEFINITION with only `indicators[0].period`
    changed - the real frozen dataclass, so what these tests rely on is the
    shape the generator really produces."""
    definition = {
        **BASE_DEFINITION,
        "indicators": [{"id": "sma_20", "type": "sma", "period": period}],
    }
    return Perturbation(
        parameter_path="indicators[0].period",
        direction=direction,
        original_value=Decimal(20),
        perturbed_value=Decimal(period),
        definition=definition,
        clamped=False,
    )


def _as_ts(day) -> datetime:
    """Midnight UTC for a fixture date - see `_replay`."""
    return datetime.combine(day, time.min, tzinfo=UTC)


def _replay(final_equity: Decimal, *, trough: Decimal | None = None):
    """A stand-in for one `_Replay`. `run_robustness_test` reads only
    `final_equity` and `equity_curve` off it (the latter solely to hand to the
    REAL `compute_max_drawdown_pct`), so a namespace carrying those two is the
    whole contract - and using one keeps this test from importing a second
    private name out of engine_v2 for no gain.

    `trough` inserts a dip so a nonzero max drawdown can be pinned; without
    it the curve rises straight from `STARTING_CASH` and the drawdown really
    is 0.
    """
    # Three-tuples since Phase 71 (D089): `_Replay.equity_curve` carries the
    # bar's own TIMESTAMP alongside its date, because an hourly backtest has
    # 24 points per calendar day and a day alone can no longer identify one.
    # The timestamps here are midnight of each date - these are daily-shaped
    # fixtures, and nothing in this test reads the instant.
    curve = [(_as_ts(START), START, STARTING_CASH)]
    if trough is not None:
        curve.append((_as_ts(START), START, trough))
    curve.append((_as_ts(END), END, final_equity))
    return SimpleNamespace(
        equity_curve=curve, round_trips=[], trades=[], final_equity=final_equity
    )


async def _run(
    *,
    perturbations: list[Perturbation],
    outcomes: dict[int, object],
    baseline: object,
    generator_error: Exception | None = None,
):
    """Runs the orchestrator with `_load_warmup_and_window` and
    `_replay_window` patched out.

    `baseline` and each entry of `outcomes` (keyed by the variant's indicator
    period) is either a `_replay(...)` namespace to return or an exception to
    raise, which is how a per-perturbation failure and a baseline failure are
    both injected. Returns `(session, run, warmup_calls)`, where `warmup_calls`
    is every `warmup_bars` value the orchestrator asked the bar provider for -
    the evidence for whether one fetch was shared or one was made per
    definition.
    """
    session = FakeSession()
    warmup_calls: list[int] = []

    async def fake_load(*, warmup_bars: int, **_kwargs):
        warmup_calls.append(warmup_bars)
        return [], []

    def fake_replay(*, definition: dict, **_kwargs):
        period = definition["indicators"][0]["period"]
        outcome = baseline if period == 20 else outcomes[period]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with (
        patch(
            "apps.api.app.backtesting.robustness._load_warmup_and_window",
            side_effect=fake_load,
        ),
        patch(
            "apps.api.app.backtesting.robustness._replay_window", side_effect=fake_replay
        ),
        patch(
            "apps.api.app.backtesting.robustness.generate_perturbed_definitions",
            side_effect=generator_error or (lambda *_a, **_kw: perturbations),
        ),
    ):
        run = await run_robustness_test(
            session=session,  # type: ignore[arg-type]
            strategy_version=SimpleNamespace(  # type: ignore[arg-type]
                id=uuid.uuid4(), definition=BASE_DEFINITION
            ),
            symbol="RBTEST.US",
            bar_interval="1d",
            start_date=START,
            end_date=END,
            starting_cash=STARTING_CASH,
            magnitude_pct=Decimal(10),
            bar_provider=SimpleNamespace(),  # type: ignore[arg-type]
            risk_limits=_risk_limits(),
            portfolio_limits=None,
            cost_model=CostModel.frictionless(),
            quantity_precision=0,
            requested_by_user_id=None,
        )
    return session, run, warmup_calls


@pytest.mark.asyncio
async def test_a_definition_with_nothing_to_perturb_is_a_failed_run() -> None:
    """An empty generator result is a real, valid outcome - `all_in` sizing
    and no indicators genuinely has no number to nudge - but it is NOT a
    succeeded run with zero perturbations, which would dress "this question
    does not apply here" up as "this strategy is insensitive"."""
    session, run, _warmups = await _run(
        perturbations=[], outcomes={}, baseline=_replay(Decimal(11_000))
    )

    assert run.status is RobustnessRunStatus.FAILED
    assert "NO_PERTURBABLE_PARAMETERS" in (run.error_detail or "")
    assert session.perturbation_rows == []
    # The baseline really ran and is recorded; only the aggregates are absent.
    assert run.baseline_return_pct == Decimal(10)
    assert run.num_perturbations is None
    assert run.mean_perturbed_return_pct is None
    assert run.stddev_perturbed_return_pct is None
    assert run.max_return_deviation_pct is None
    assert run.completed_at is not None


@pytest.mark.asyncio
async def test_aggregates_cover_only_the_succeeded_perturbations() -> None:
    """Four variants, one of which cannot be replayed. Every expected number
    is hand-derivable from the injected final equities:

    baseline 11,000 -> +10%; variants 10,500 -> +5%, 9,000 -> -10%,
    12,000 -> +20%, and one InsufficientHistoryError.

    mean(5, -10, 20) = 5; sample stdev = sqrt((0 + 225 + 225) / 2) = 15;
    max |deviation from +10| = max(5, 20, 10) = 20.

    The failed variant is deliberately the one that would drag every one of
    those toward zero if it were counted as 0%.
    """
    perturbations = [
        _perturbation(22, "+"),
        _perturbation(18, "-"),
        _perturbation(24, "+"),
        _perturbation(16, "-"),
    ]
    session, run, _warmups = await _run(
        perturbations=perturbations,
        outcomes={
            22: _replay(Decimal(10_500)),
            18: _replay(Decimal(9_000)),
            24: _replay(Decimal(12_000)),
            16: InsufficientHistoryError("seeded gap"),
        },
        baseline=_replay(Decimal(11_000)),
    )

    assert run.status is RobustnessRunStatus.SUCCEEDED
    assert run.error_detail is None
    assert run.baseline_return_pct == Decimal(10)

    # Counts are observations: everything attempted, and how many answered.
    assert run.num_perturbations == 4
    assert run.num_succeeded_perturbations == 3

    assert run.mean_perturbed_return_pct == Decimal(5)
    assert run.stddev_perturbed_return_pct == Decimal(15)
    assert run.max_return_deviation_pct == Decimal(20)

    # Every attempted perturbation is persisted, succeeded or not - the gap is
    # visible rather than smoothed over.
    rows = session.perturbation_rows
    assert len(rows) == 4
    succeeded = [row for row in rows if row.status == "succeeded"]
    failed = [row for row in rows if row.status == "failed"]
    assert len(succeeded) == 3
    assert len(failed) == 1
    assert sorted(row.total_return_pct for row in succeeded) == [
        Decimal(-10),
        Decimal(5),
        Decimal(20),
    ]
    # A failed perturbation has no metrics at all - NULL, never 0.
    assert failed[0].perturbed_value == Decimal(16)
    assert failed[0].total_return_pct is None
    assert failed[0].max_drawdown_pct is None
    assert "seeded gap" in (failed[0].error_detail or "")


@pytest.mark.asyncio
async def test_every_perturbation_failing_still_leaves_a_succeeded_run() -> None:
    """The baseline ran, and every nudge of it fell over. That is a real -
    arguably the strongest - finding about parameter sensitivity, so the run
    SUCCEEDS with the baseline's own numbers and the count of what was
    attempted, and with NULL aggregates because nothing was aggregated."""
    perturbations = [_perturbation(22, "+"), _perturbation(18, "-")]
    session, run, _warmups = await _run(
        perturbations=perturbations,
        outcomes={
            22: InsufficientHistoryError("no warmup for 22"),
            18: InsufficientHistoryError("no warmup for 18"),
        },
        baseline=_replay(Decimal(11_000), trough=Decimal(8_000)),
    )

    assert run.status is RobustnessRunStatus.SUCCEEDED
    assert run.error_detail is None
    assert run.baseline_return_pct == Decimal(10)
    # 10,000 -> 8,000 is a 20% peak-to-trough decline, computed by the real
    # metrics function rather than asserted as a magic number.
    assert run.baseline_max_drawdown_pct == Decimal(20)
    assert run.num_perturbations == 2
    assert run.num_succeeded_perturbations == 0
    assert run.mean_perturbed_return_pct is None
    assert run.stddev_perturbed_return_pct is None
    assert run.max_return_deviation_pct is None
    assert [row.status for row in session.perturbation_rows] == ["failed", "failed"]


@pytest.mark.asyncio
async def test_a_failed_baseline_fails_the_whole_run() -> None:
    """Without the baseline there is nothing for a perturbation to be
    measured against, so this is the one replay failure that ends the run -
    and it ends it before the generator is even consulted."""
    session, run, warmups = await _run(
        perturbations=[_perturbation(22, "+")],
        outcomes={},
        baseline=InsufficientHistoryError("only 4 bars precede 2026-01-05"),
    )

    assert run.status is RobustnessRunStatus.FAILED
    assert "BASELINE_FAILED" in (run.error_detail or "")
    # The real underlying message survives into the row, not just a label.
    assert "only 4 bars precede" in (run.error_detail or "")
    assert run.baseline_return_pct is None
    assert run.num_perturbations is None
    assert session.perturbation_rows == []
    # Exactly one attempted load: the baseline's. Nothing was perturbed.
    assert warmups == [21]


@pytest.mark.asyncio
async def test_one_succeeded_perturbation_has_a_mean_but_no_stddev() -> None:
    """One observation genuinely has no spread. Leaving `stddev` NULL says
    that; `0.0000` would say "it never varied", which is a different and
    false claim - the same rule walk-forward's single-window case follows."""
    session, run, _warmups = await _run(
        perturbations=[_perturbation(22, "+"), _perturbation(18, "-")],
        outcomes={
            22: _replay(Decimal(10_500)),
            18: InsufficientHistoryError("seeded gap"),
        },
        baseline=_replay(Decimal(11_000)),
    )

    assert run.status is RobustnessRunStatus.SUCCEEDED
    assert run.num_succeeded_perturbations == 1
    assert run.mean_perturbed_return_pct == Decimal(5)
    assert run.stddev_perturbed_return_pct is None
    # Still a real deviation: one measured variant is enough to say how far
    # the result moved, even though it is not enough to say how it scatters.
    assert run.max_return_deviation_pct == Decimal(5)


@pytest.mark.asyncio
async def test_warmup_is_recomputed_for_every_perturbed_definition() -> None:
    """The correctness decision this module documents, pinned as behaviour:
    a perturbed period changes how much warmup its own replay needs, so the
    bar store is asked once per definition rather than once for the run.

    Sharing the baseline's 21-bar warmup with the period-22 variant would
    hand `_replay_window` too few leading bars and produce a number for a
    variant that was never really replayed - a silent wrong answer rather
    than a loud failure.
    """
    _session, run, warmups = await _run(
        perturbations=[_perturbation(22, "+"), _perturbation(18, "-")],
        outcomes={22: _replay(Decimal(11_000)), 18: _replay(Decimal(11_000))},
        baseline=_replay(Decimal(11_000)),
    )

    assert run.status is RobustnessRunStatus.SUCCEEDED
    # `_warmup_bar_count` is max(period) + 1, and it is the REAL function
    # here: 20 -> 21, 22 -> 23, 18 -> 19.
    assert warmups == [21, 23, 19]


@pytest.mark.asyncio
async def test_a_generator_failure_is_recorded_rather_than_raised() -> None:
    """The generator is pure and validated input should not break it, but the
    row already exists to record it if it does - which beats a 500 that
    strands the row in RUNNING forever."""
    _session, run, _warmups = await _run(
        perturbations=[],
        outcomes={},
        baseline=_replay(Decimal(11_000)),
        generator_error=ValueError("unexpected sizing shape"),
    )

    assert run.status is RobustnessRunStatus.FAILED
    assert "PERTURBATION_GENERATION_FAILED" in (run.error_detail or "")
    assert "unexpected sizing shape" in (run.error_detail or "")
