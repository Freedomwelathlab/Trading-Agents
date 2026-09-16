"""The strategy improvement loop's anti-overfitting rules (Phase 72, D090).
DB-backed, against real Postgres.

**What is actually under test.** Not "does the search find a better
strategy" - that depends entirely on the data and is not a property of the
code. What is asserted is the DISCIPLINE: that the held-out window is
consulted only when a candidate has already won on train, that acceptance
requires improving on both, that a train-only improvement is rejected as
overfitting and stops the search, and that every one of those outcomes is
recorded with a stated reason.

`run_strategy_backtest` is patched in every test here so each candidate's
return is INJECTED. That is deliberate and is not a weakening: the engine
has its own tests, and letting real replays decide the numbers would mean
these tests could only assert whatever the fixture data happened to
produce - which is the opposite of pinning a decision rule. Injecting the
returns is what lets "better on train, worse on held-out" be constructed
exactly, which is the case that matters most and the least likely to occur
on demand in real bars.
"""

import contextlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select

from apps.api.app.backtesting.costs import CostModel
from apps.api.app.db.base import get_session
from apps.api.app.db.models import (
    BacktestRun,
    BacktestRunStatus,
    Strategy,
    StrategyImprovementRun,
    StrategyImprovementRunStatus,
    StrategyImprovementStep,
    StrategyVersion,
    StrategyVersionStatus,
)
from apps.api.app.risk.models import RiskLimits
from apps.api.app.strategies.improvement import _json_native, run_improvement_loop
from apps.api.app.strategies.service import compute_definition_hash

SYMBOL = "IMPROVETEST.US"
TRAIN = (date(2026, 1, 1), date(2026, 3, 31))
VALIDATE = (date(2026, 4, 1), date(2026, 4, 30))

BASE_DEFINITION = {
    "indicators": [
        {"id": "ema_20", "type": "ema", "period": 20},
        {"id": "ema_50", "type": "ema", "period": 50},
    ],
    "entry_rule": {"op": "crosses_above", "left": "ema_20", "right": "ema_50"},
    "exit_rule": {"op": "crosses_below", "left": "ema_20", "right": "ema_50"},
    "position_sizing": {"type": "fixed_fraction", "fraction": 0.09},
}

ALL_IN_DEFINITION = {
    "indicators": [],
    "entry_rule": {"op": "gt", "left": "close", "right": 1},
    "exit_rule": {"op": "lt", "left": "close", "right": 1},
    "position_sizing": {"type": "all_in"},
}


def _limits() -> RiskLimits:
    return RiskLimits(
        max_position_pct_of_equity=Decimal("0.30"),
        max_portfolio_exposure_pct_of_equity=Decimal(1),
        max_risk_pct_of_equity_per_trade=Decimal("0.01"),
        require_stop_price=False,
        max_market_data_age_seconds=86_400,
        duplicate_order_window_seconds=5,
    )


@contextlib.asynccontextmanager
async def db_session():
    gen = get_session()
    session = await anext(gen)
    try:
        yield session
    finally:
        await gen.aclose()


@contextlib.asynccontextmanager
async def strategy_with_base(session, definition: dict = BASE_DEFINITION):
    strategy_id, version_id = uuid.uuid4(), uuid.uuid4()
    session.add(Strategy(id=strategy_id, owner_user_id=None, name=f"improve-{strategy_id}"))
    session.add(
        StrategyVersion(
            id=version_id,
            strategy_id=strategy_id,
            version_number=1,
            definition=definition,
            definition_hash=compute_definition_hash(definition),
            status=StrategyVersionStatus.VALIDATED,
            validated_at=datetime.now(UTC),
        )
    )
    await session.commit()
    version = await session.get(StrategyVersion, version_id)
    try:
        yield strategy_id, version
    finally:
        await session.rollback()
        run_ids = select(StrategyImprovementRun.id).where(
            StrategyImprovementRun.strategy_id == strategy_id
        )
        await session.execute(
            delete(StrategyImprovementStep).where(
                StrategyImprovementStep.improvement_run_id.in_(run_ids)
            )
        )
        await session.execute(
            delete(StrategyImprovementRun).where(
                StrategyImprovementRun.strategy_id == strategy_id
            )
        )
        version_ids = select(StrategyVersion.id).where(
            StrategyVersion.strategy_id == strategy_id
        )
        await session.execute(
            delete(BacktestRun).where(BacktestRun.strategy_version_id.in_(version_ids))
        )
        await session.execute(
            delete(StrategyVersion).where(StrategyVersion.strategy_id == strategy_id)
        )
        await session.execute(delete(Strategy).where(Strategy.id == strategy_id))
        await session.commit()


def _fake_run(session, version_id: uuid.UUID, return_pct: Decimal | None) -> BacktestRun:
    """A committed BacktestRun with an injected return. `None` makes it
    FAILED, which is how a candidate that could not be replayed is
    expressed."""
    run = BacktestRun(
        id=uuid.uuid4(),
        strategy_version_id=version_id,
        symbol=SYMBOL,
        bar_interval="1d",
        start_date=TRAIN[0],
        end_date=TRAIN[1],
        starting_cash=Decimal(10_000),
        status=(
            BacktestRunStatus.SUCCEEDED if return_pct is not None else BacktestRunStatus.FAILED
        ),
        total_return_pct=return_pct,
        error_detail=None if return_pct is not None else "seeded failure",
        completed_at=datetime.now(UTC),
    )
    session.add(run)
    return run


class _Injector:
    """Returns a scripted result per (window, call order).

    Keyed on whether the requested window is the TRAIN or the VALIDATE one,
    so a test can say "every train candidate returns X, the validation
    returns Y" without caring how many candidates the perturbation
    generator happens to produce.
    """

    def __init__(self, *, train: list[Decimal | None], validate: list[Decimal | None]):
        self.train = list(train)
        self.validate = list(validate)
        self.validate_calls = 0
        self.train_calls = 0

    async def __call__(self, **kwargs):
        session = kwargs["session"]
        version = kwargs["strategy_version"]
        is_validate = kwargs["start_date"] == VALIDATE[0]
        if is_validate:
            self.validate_calls += 1
            value = self.validate.pop(0) if self.validate else self.validate_last
            self.validate_last = value
        else:
            self.train_calls += 1
            value = self.train.pop(0) if self.train else self.train_last
            self.train_last = value
        run = _fake_run(session, version.id, value)
        await session.flush()
        return run

    train_last: Decimal | None = Decimal(0)
    validate_last: Decimal | None = Decimal(0)


async def _run(session, strategy_id, base, injector, *, max_iterations=3):
    # `new=`, not `side_effect=`: the injector IS an async callable, and
    # handing it to an AsyncMock as a side effect makes the mock return the
    # un-awaited coroutine. Replacing the name outright is both simpler and
    # closer to what the module actually calls.
    with patch(
        "apps.api.app.strategies.improvement.run_strategy_backtest", new=injector
    ):
        return await run_improvement_loop(
            session=session,
            strategy_id=strategy_id,
            base_version=base,
            symbol=SYMBOL,
            bar_interval="1d",
            train_start_date=TRAIN[0],
            train_end_date=TRAIN[1],
            validate_start_date=VALIDATE[0],
            validate_end_date=VALIDATE[1],
            starting_cash=Decimal(10_000),
            max_iterations=max_iterations,
            bar_provider=None,  # type: ignore[arg-type]
            risk_limits=_limits(),
            portfolio_limits=None,
            cost_model=CostModel.frictionless(),
            quantity_precision=0,
            requested_by_user_id=None,
        )


# ------------------------------------------------- the central discipline


@pytest.mark.asyncio
async def test_a_train_only_improvement_is_rejected_as_overfitting() -> None:
    """The case the whole module exists for. A candidate beats the baseline
    on the window the search was allowed to look at, and does NOT beat it on
    the window it was not. That is the signature of fitting noise, and it
    must not be accepted however good the train number looks."""
    async with db_session() as session, strategy_with_base(session) as (sid, base):
        injector = _Injector(
            # baseline train 1%, then every candidate 5% (all better)
            train=[Decimal(1)] + [Decimal(5)] * 12,
            # baseline validate 3%, the winner's validate 2% (worse)
            validate=[Decimal(3), Decimal(2)],
        )
        run = await _run(session, sid, base, injector)

        assert run.status is StrategyImprovementRunStatus.SUCCEEDED
        assert run.best_version_id is None, "an overfit candidate must not become the best"
        assert run.best_validate_return_pct == Decimal("3.0000")

        step = (
            await session.execute(
                select(StrategyImprovementStep).where(
                    StrategyImprovementStep.improvement_run_id == run.id
                )
            )
        ).scalars().all()[-1]
        assert step.accepted is False
        assert "OVERFIT_REJECTED" in step.accepted_reason


@pytest.mark.asyncio
async def test_the_holdout_is_not_consulted_when_no_candidate_wins_on_train() -> None:
    """The hold-out budget. A window judged against repeatedly stops being
    held out, so it is touched only for a candidate that has already earned
    it - never speculatively, and never for a candidate the search is going
    to discard anyway."""
    async with db_session() as session, strategy_with_base(session) as (sid, base):
        injector = _Injector(
            train=[Decimal(10)] + [Decimal(1)] * 12,  # baseline best, candidates worse
            validate=[Decimal(5)],  # baseline only
        )
        run = await _run(session, sid, base, injector)

        assert run.status is StrategyImprovementRunStatus.SUCCEEDED
        # Exactly ONE validation call: the baseline's. No candidate earned one.
        assert injector.validate_calls == 1
        step = (
            await session.execute(
                select(StrategyImprovementStep).where(
                    StrategyImprovementStep.improvement_run_id == run.id
                )
            )
        ).scalars().all()[-1]
        assert step.validate_backtest_run_id is None
        assert "CONVERGED" in step.accepted_reason
        assert "held-out window was NOT consulted" in step.accepted_reason


@pytest.mark.asyncio
async def test_a_candidate_better_on_both_windows_is_accepted() -> None:
    """The positive case: improvement is real only when it survives data the
    search never optimised against."""
    async with db_session() as session, strategy_with_base(session) as (sid, base):
        injector = _Injector(
            train=[Decimal(1)] + [Decimal(5)] * 12,
            # baseline 3%, then the winner 7% - better out-of-sample too
            validate=[Decimal(3), Decimal(7)],
        )
        run = await _run(session, sid, base, injector, max_iterations=1)

        assert run.status is StrategyImprovementRunStatus.SUCCEEDED
        assert run.best_version_id is not None
        assert run.best_version_id != base.id
        assert run.best_train_return_pct == Decimal("5.0000")
        assert run.best_validate_return_pct == Decimal("7.0000")

        step = (
            await session.execute(
                select(StrategyImprovementStep).where(
                    StrategyImprovementStep.improvement_run_id == run.id
                )
            )
        ).scalars().all()[-1]
        assert step.accepted is True
        assert "ACCEPTED" in step.accepted_reason
        # A winner - and ONLY a winner - leaves a version behind.
        assert step.created_version_id is not None


@pytest.mark.asyncio
async def test_a_rejected_candidate_leaves_no_version_to_deploy_by_mistake() -> None:
    """`created_version_id` is set for accepted candidates only, so a
    rejected variant cannot be mistaken for something the search endorsed."""
    async with db_session() as session, strategy_with_base(session) as (sid, base):
        injector = _Injector(
            train=[Decimal(1)] + [Decimal(5)] * 12,
            validate=[Decimal(3), Decimal(2)],
        )
        run = await _run(session, sid, base, injector)

        steps = (
            await session.execute(
                select(StrategyImprovementStep).where(
                    StrategyImprovementStep.improvement_run_id == run.id
                )
            )
        ).scalars().all()
        assert all(s.created_version_id is None for s in steps if not s.accepted)


# ----------------------------------------------------- honest failures


@pytest.mark.asyncio
async def test_a_baseline_that_cannot_be_replayed_fails_the_whole_search() -> None:
    """With no baseline there is nothing to improve ON, and "better than
    nothing" is not a claim this system will make."""
    async with db_session() as session, strategy_with_base(session) as (sid, base):
        injector = _Injector(train=[None], validate=[Decimal(1)])
        run = await _run(session, sid, base, injector)

        assert run.status is StrategyImprovementRunStatus.FAILED
        assert run.error_detail and "BASELINE_FAILED" in run.error_detail
        assert run.best_version_id is None


@pytest.mark.asyncio
async def test_a_definition_with_no_numeric_parameter_says_so() -> None:
    """`all_in` sizing with no indicators has nothing to vary. That is a
    real, complete answer rather than an error or an empty success."""
    async with (
        db_session() as session,
        strategy_with_base(session, ALL_IN_DEFINITION) as (sid, base),
    ):
        injector = _Injector(train=[Decimal(1)], validate=[Decimal(1)])
        run = await _run(session, sid, base, injector)

        assert run.status is StrategyImprovementRunStatus.SUCCEEDED
        assert run.best_version_id is None
        step = (
            await session.execute(
                select(StrategyImprovementStep).where(
                    StrategyImprovementStep.improvement_run_id == run.id
                )
            )
        ).scalars().one()
        assert "NO_PERTURBABLE_PARAMETERS" in step.accepted_reason


@pytest.mark.asyncio
async def test_the_baseline_figures_are_always_recorded() -> None:
    """Whatever the search concludes, what it started from is part of the
    record - otherwise "improved to 5%" cannot be read as an improvement."""
    async with db_session() as session, strategy_with_base(session) as (sid, base):
        injector = _Injector(
            train=[Decimal("1.5")] + [Decimal(1)] * 12,
            validate=[Decimal("2.5")],
        )
        run = await _run(session, sid, base, injector)

        assert run.baseline_train_return_pct == Decimal("1.5000")
        assert run.baseline_validate_return_pct == Decimal("2.5000")
        assert run.candidates_tested > 0


# ------------------------------------------- the JSONB boundary conversion


def test_an_integral_decimal_becomes_an_int_not_a_float() -> None:
    """An indicator period must stay a period. `20.0` in a definition would
    be legal JSON and wrong-looking forever after."""
    out = _json_native({"indicators": [{"period": Decimal(20)}]})
    period = out["indicators"][0]["period"]
    assert period == 20
    assert isinstance(period, int)
    assert not isinstance(period, float)


def test_a_fractional_decimal_round_trips_through_the_stored_float_exactly() -> None:
    """The property that makes converting at the write safe rather than a
    quiet precision loss.

    `perturbation.py` deliberately computes an EXACT Decimal - 0.25 * 0.9
    is precisely 0.225, not binary float's 0.225000000000000005... - and
    that in-memory contract is pinned by its own tests. This asserts the
    other half: storing it as a float and reading it back the way every
    consumer does (`Decimal(str(...))`, see `engine_v2._desired_quantity`)
    returns the same exact decimal.
    """
    exact = Decimal("0.25") * Decimal("0.9")
    assert exact == Decimal("0.225")

    stored = _json_native({"position_sizing": {"fraction": exact}})
    fraction = stored["position_sizing"]["fraction"]
    assert isinstance(fraction, float)
    assert Decimal(str(fraction)) == Decimal("0.225")


def test_values_that_are_already_json_native_are_left_untouched() -> None:
    """One-factor-at-a-time depends on it: a variant perturbing a PERIOD
    must not also nudge the fraction on its way through the converter."""
    original = {
        "indicators": [{"id": "sma_20", "type": "sma", "period": 20}],
        "position_sizing": {"type": "fixed_fraction", "fraction": 0.09},
        "entry_rule": {"op": "gt", "left": "close", "right": 1},
    }
    assert _json_native(original) == original


def test_the_whole_definition_is_json_serializable_after_conversion() -> None:
    """The failure this exists to prevent, stated directly: a Decimal in a
    JSONB column raises "Object of type Decimal is not JSON serializable"
    at the write, after the search has already done all its work."""
    import json

    definition = {
        "indicators": [{"id": "e", "type": "ema", "period": Decimal(22)}],
        "entry_rule": {"op": "gt", "left": "close", "right": "e"},
        "exit_rule": {"op": "lt", "left": "close", "right": "e"},
        "position_sizing": {"type": "fixed_fraction", "fraction": Decimal("0.081")},
    }
    with pytest.raises(TypeError):
        json.dumps(definition)
    assert json.loads(json.dumps(_json_native(definition))) is not None
