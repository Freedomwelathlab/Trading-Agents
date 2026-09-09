"""The Phase 57 walk-forward orchestrator's own mechanics: window-boundary
computation, the two-window minimum, and the aggregate statistics.

**`run_strategy_backtest` is patched out in every test here, deliberately,
and there is no database.** This module orchestrates the engine; it is not
the engine's test (tests/backtesting/test_engine_v2.py is, against a real
Postgres). Injecting hand-crafted per-window results instead makes every
expected number below hand-derivable from the injected returns alone - which
is what lets the mean, the sample standard deviation and the best/worst
pair be pinned exactly rather than being re-derived from whatever the
evaluator happens to produce. The DB-backed half - real bars, real windows,
real persisted rows - is tests/api/test_walk_forward.py's job.

The session is a hand-written fake for the same reason: `run_walk_forward`
only ever calls `add` / `flush` / `commit` / `refresh` on it, so a fake
records exactly what would have been written without needing a live
connection, and the window rows it collects are asserted on directly.
"""

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.api.app.backtesting.walk_forward import (
    MIN_WINDOWS,
    compute_windows,
    run_walk_forward,
)
from apps.api.app.db.models import (
    BacktestRunStatus,
    WalkForwardRun,
    WalkForwardRunStatus,
    WalkForwardWindow,
)
from apps.api.app.risk.models import RiskLimits

START = date(2026, 1, 1)


class FakeSession:
    """Records what would have been written. `flush` and `refresh` are
    no-ops because nothing here has a database to round-trip through; that
    NUMERIC(10,4) rounding really happens is the DB-backed test's assertion,
    not this module's."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: object) -> None:
        return None

    @property
    def walk_forward_run(self) -> WalkForwardRun:
        runs = [obj for obj in self.added if isinstance(obj, WalkForwardRun)]
        assert len(runs) == 1
        return runs[0]

    @property
    def windows(self) -> list[WalkForwardWindow]:
        return [obj for obj in self.added if isinstance(obj, WalkForwardWindow)]


def _risk_limits() -> RiskLimits:
    """Never actually consulted - `run_strategy_backtest` is patched out -
    but passed through as the real type so the call signature is exercised
    exactly as the route builds it."""
    return RiskLimits(
        max_position_pct_of_equity=Decimal("0.30"),
        max_portfolio_exposure_pct_of_equity=Decimal(1),
        max_risk_pct_of_equity_per_trade=Decimal("0.01"),
        require_stop_price=False,
        max_market_data_age_seconds=86_400,
        duplicate_order_window_seconds=5,
    )


def _fake_backtest_run(total_return_pct: str | None, *, error: str | None = None):
    """A stand-in for one committed `BacktestRun`. `total_return_pct=None`
    means that window's own backtest FAILED, which is exactly the case the
    aggregates must skip rather than treat as 0%."""
    failed = total_return_pct is None
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=BacktestRunStatus.FAILED if failed else BacktestRunStatus.SUCCEEDED,
        total_return_pct=None if failed else Decimal(total_return_pct),
        error_detail=error if failed else None,
    )


async def _run(returns: list[str | None], *, window_days: int, end: date):
    """Runs the orchestrator with one injected per-window result per planned
    window, in order, and returns `(session, run, fake, injected_results)`."""
    session = FakeSession()
    results = [_fake_backtest_run(value, error="seeded gap") for value in returns]
    with patch(
        "apps.api.app.backtesting.walk_forward.run_strategy_backtest",
        side_effect=list(results),
    ) as fake:
        run = await run_walk_forward(
            session=session,  # type: ignore[arg-type]
            strategy_version=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
            symbol="WFTEST.US",
            bar_interval="1d",
            overall_start_date=START,
            overall_end_date=end,
            window_days=window_days,
            starting_cash=Decimal(10_000),
            bar_provider=SimpleNamespace(),  # type: ignore[arg-type]
            risk_limits=_risk_limits(),
            portfolio_limits=None,
            requested_by_user_id=None,
        )
    return session, run, fake, results


def test_windows_are_sequential_non_overlapping_and_inclusive() -> None:
    """The boundary convention, pinned: window `i` is
    `[start + i*window_days, start + (i+1)*window_days - 1]`, both endpoints
    inclusive, so consecutive windows touch without overlapping and the
    series has no gaps."""
    windows = compute_windows(
        overall_start_date=date(2026, 1, 1),
        overall_end_date=date(2026, 1, 21),
        window_days=7,
    )
    assert [(w.index, w.start_date, w.end_date) for w in windows] == [
        (0, date(2026, 1, 1), date(2026, 1, 7)),
        (1, date(2026, 1, 8), date(2026, 1, 14)),
        (2, date(2026, 1, 15), date(2026, 1, 21)),
    ]
    assert all(w.day_count == 7 for w in windows)


def test_a_long_enough_partial_tail_window_is_kept() -> None:
    """Jan 1 - Jan 26 at 10 days: two full windows plus a 6-day tail. Six is
    at least `10 // 2`, so the tail is real enough to count."""
    windows = compute_windows(
        overall_start_date=date(2026, 1, 1),
        overall_end_date=date(2026, 1, 26),
        window_days=10,
    )
    assert [(w.start_date, w.end_date) for w in windows] == [
        (date(2026, 1, 1), date(2026, 1, 10)),
        (date(2026, 1, 11), date(2026, 1, 20)),
        (date(2026, 1, 21), date(2026, 1, 26)),
    ]
    assert windows[-1].day_count == 6


def test_a_too_short_partial_tail_window_is_dropped() -> None:
    """Jan 1 - Jan 24 at 10 days leaves a 4-day tail, under `10 // 2`. It is
    dropped silently: a quarter-length window's return would sit in the same
    mean and the same standard deviation as a full one while measuring far
    less."""
    windows = compute_windows(
        overall_start_date=date(2026, 1, 1),
        overall_end_date=date(2026, 1, 24),
        window_days=10,
    )
    assert [(w.start_date, w.end_date) for w in windows] == [
        (date(2026, 1, 1), date(2026, 1, 10)),
        (date(2026, 1, 11), date(2026, 1, 20)),
    ]


def test_no_window_ever_extends_past_the_overall_end_date() -> None:
    for end in (date(2026, 1, 20), date(2026, 1, 26), date(2026, 3, 15)):
        for window_days in (5, 7, 30):
            for window in compute_windows(
                overall_start_date=START, overall_end_date=end, window_days=window_days
            ):
                assert window.start_date >= START
                assert window.end_date <= end
                assert window.start_date <= window.end_date


@pytest.mark.asyncio
async def test_fewer_than_two_windows_is_a_failed_run_naming_the_count() -> None:
    """One window says nothing about consistency. The run still exists - it
    is a real, auditable record of what was asked for - and its error names
    the actual count and the needed one."""
    session, run, fake, _results = await _run([], window_days=30, end=date(2026, 1, 20))

    assert fake.call_count == 0
    assert run.status is WalkForwardRunStatus.FAILED
    assert "INSUFFICIENT_WINDOWS" in (run.error_detail or "")
    assert "1 window(s)" in (run.error_detail or "")
    assert f"at least {MIN_WINDOWS}" in (run.error_detail or "")
    assert run.completed_at is not None
    # Nothing was attempted, so nothing was counted - NULL, never 0.
    assert run.num_windows is None
    assert run.num_succeeded_windows is None
    assert run.mean_return_pct is None
    assert session.windows == []


@pytest.mark.asyncio
async def test_aggregates_are_computed_over_succeeded_windows_only() -> None:
    """Four windows, one of which failed. The failed one is COUNTED in
    `num_windows` and gets its own window row, but contributes nothing to the
    statistics - not a zero.

    Injected returns: 10, -4, (failed), 6.
      mean   = (10 - 4 + 6) / 3 = 4
      stdev  = sqrt(((10-4)^2 + (-4-4)^2 + (6-4)^2) / (3-1))
             = sqrt((36 + 64 + 4) / 2) = sqrt(52)
      best   = 10, worst = -4, profitable = 2 (10 and 6)
    """
    session, run, fake, results = await _run(
        ["10", "-4", None, "6"], window_days=7, end=date(2026, 1, 28)
    )

    assert fake.call_count == 4
    assert run.status is WalkForwardRunStatus.SUCCEEDED
    assert run.error_detail is None
    assert run.num_windows == 4
    assert run.num_succeeded_windows == 3
    assert run.num_profitable_windows == 2
    assert run.mean_return_pct == Decimal(4)
    assert abs((run.stddev_return_pct or Decimal(0)) - Decimal(52).sqrt()) < Decimal("1e-9")
    assert run.best_window_return_pct == Decimal(10)
    assert run.worst_window_return_pct == Decimal(-4)

    # One window row per attempted window, indexed from 0, each pointing at
    # the real run the engine returned - including the failed one.
    assert [w.window_index for w in session.windows] == [0, 1, 2, 3]
    assert [w.backtest_run_id for w in session.windows] == [r.id for r in results]
    assert [(w.start_date, w.end_date) for w in session.windows] == [
        (date(2026, 1, 1), date(2026, 1, 7)),
        (date(2026, 1, 8), date(2026, 1, 14)),
        (date(2026, 1, 15), date(2026, 1, 21)),
        (date(2026, 1, 22), date(2026, 1, 28)),
    ]


@pytest.mark.asyncio
async def test_each_window_is_backtested_over_its_own_inclusive_dates() -> None:
    """Every window is a real `run_strategy_backtest` call over that
    window's own `[start_date, end_date]`, each starting fresh at the SAME
    `starting_cash` rather than compounding through a running balance."""
    _session, _wf_run, fake, _results = await _run(
        ["1", "2"], window_days=7, end=date(2026, 1, 14)
    )

    passed = [call.kwargs for call in fake.call_args_list]
    assert [(kw["start_date"], kw["end_date"]) for kw in passed] == [
        (date(2026, 1, 1), date(2026, 1, 7)),
        (date(2026, 1, 8), date(2026, 1, 14)),
    ]
    assert {kw["starting_cash"] for kw in passed} == {Decimal(10_000)}
    assert {kw["symbol"] for kw in passed} == {"WFTEST.US"}


@pytest.mark.asyncio
async def test_all_windows_failed_is_a_failed_run_quoting_the_first_reason() -> None:
    """Zero succeeded windows means nothing to aggregate. The error names
    the count AND the first window's own reason, so a caller gets a concrete
    lead rather than "everything failed"."""
    session, run, _fake, _results = await _run(
        [None, None, None], window_days=7, end=date(2026, 1, 21)
    )

    assert run.status is WalkForwardRunStatus.FAILED
    assert "ALL_WINDOWS_FAILED" in (run.error_detail or "")
    assert "seeded gap" in (run.error_detail or "")
    # Counts are real observations even here: 3 attempted, 0 succeeded.
    assert run.num_windows == 3
    assert run.num_succeeded_windows == 0
    # Statistics over an empty set are not 0, they are absent.
    assert run.mean_return_pct is None
    assert run.stddev_return_pct is None
    assert run.best_window_return_pct is None
    assert run.worst_window_return_pct is None
    # Every attempted window is still recorded, pointing at its failed run.
    assert len(session.windows) == 3


@pytest.mark.asyncio
async def test_a_single_succeeded_window_has_no_standard_deviation() -> None:
    """Two windows attempted, one succeeded. `statistics.stdev` raises on
    n < 2, so this is the guarded edge case: the column is NULL, which says
    "one observation has no spread" rather than the very different claim
    `0.0000` would make."""
    _session, run, _fake, _results = await _run(
        ["7.5", None], window_days=7, end=date(2026, 1, 14)
    )

    assert run.status is WalkForwardRunStatus.SUCCEEDED
    assert run.num_windows == 2
    assert run.num_succeeded_windows == 1
    assert run.num_profitable_windows == 1
    assert run.mean_return_pct == Decimal("7.5")
    assert run.stddev_return_pct is None
    assert run.best_window_return_pct == Decimal("7.5")
    assert run.worst_window_return_pct == Decimal("7.5")


@pytest.mark.asyncio
async def test_an_unexpected_failure_mid_run_resolves_the_row_rather_than_raising() -> None:
    """`run_strategy_backtest` never raises (D072), so reaching the broad
    handler means something outside a window's replay broke. The already
    created row must still finish - a stranded RUNNING row would be worse
    than any exception."""
    session = FakeSession()
    with patch(
        "apps.api.app.backtesting.walk_forward.run_strategy_backtest",
        side_effect=RuntimeError("bar store connection dropped"),
    ):
        run = await run_walk_forward(
            session=session,  # type: ignore[arg-type]
            strategy_version=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
            symbol="WFTEST.US",
            bar_interval="1d",
            overall_start_date=START,
            overall_end_date=date(2026, 1, 14),
            window_days=7,
            starting_cash=Decimal(10_000),
            bar_provider=SimpleNamespace(),  # type: ignore[arg-type]
            risk_limits=_risk_limits(),
            portfolio_limits=None,
            requested_by_user_id=None,
        )

    assert run.status is WalkForwardRunStatus.FAILED
    assert "bar store connection dropped" in (run.error_detail or "")
    assert run.completed_at is not None
    assert session.commits >= 1
