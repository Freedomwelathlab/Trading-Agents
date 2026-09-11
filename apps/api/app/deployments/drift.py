"""Drift detection for a strategy deployment (Phase 66, D084) - after each
successful runner cycle, ask whether this deployment's REAL closed-trade
performance has drifted meaningfully from its own REAL backtest, and hand
back a typed verdict. This module computes nothing new about performance
itself; it reuses Phase 65's `build_deployment_monitoring` verbatim and
compares two numbers that function already produces honestly.

WHY WIN-RATE DEVIATION ON CLOSED ROUND TRIPS IS THE DRIFT SIGNAL FOR v1
------------------------------------------------------------------------
`build_deployment_monitoring` already computes `actual.win_rate_pct` (from
this deployment's own real fills) and `expected.win_rate_pct` (from the
strategy version's own latest succeeded backtest) - the one pair of numbers
on both sides of the actual/expected comparison that is honest today,
requires no new query, and cannot disagree with what the monitoring screen
already shows an operator. Comparing them is the smallest signal that
answers "is live behavior diverging from the backtest," which is exactly
what this phase is for.

**A known, deliberate limitation, named rather than glossed over**: the
more informative drift signal would compare the DISTRIBUTION of signals (or
returns) the live deployment produced against the distribution the backtest
produced over the same stretch of history - not just two win-rate scalars.
Building that would need per-bar backtest signal data persisted somewhere
queryable, and the backtest engine (`backtesting/engine_v2.py`) does not
persist one - it produces a `BacktestRun` summary, not a bar-by-bar signal
log. Building that persistence is out of scope for this phase; a
signal-distribution comparison is future work, not something this module
pretends to already do by computing a coarser number instead.

WHY AUTO-PAUSE DEFAULTS OFF
----------------------------
`strategy_drift_auto_pause_enabled` (`core/config.py`) defaults `False` -
the same fail-closed posture as the snapshot scheduler, the reconciler, the
strategy runner itself, and live trading (docs/TRADING_SAFETY.md). Drift is
detected and recorded in `strategy_drift_checks` every cycle regardless;
only the decision to call `deployments/service.py::pause_deployment` for the
operator is gated by the setting. This feature can only ever make a
deployment MORE conservative (pause it) - it never places or sizes a trade
differently - but "can only make things safer" is not the same as "should
default to acting automatically," and this codebase's standing rule is that
automation stays opt-in even when the action it takes is a conservative one.

WHY THIS RUNS ONCE PER SUCCESSFUL RUNNER CYCLE, NOT ON ITS OWN SCHEDULE
-------------------------------------------------------------------------
`evaluate_deployment_drift` is called from inside
`deployments/runner.py::_run_deployment_isolated`, in the SAME transaction
as the trading cycle it evaluates - reusing the runner's existing per-
deployment lock, gate, and transaction boundary rather than standing up a
second scheduler, a second advisory-lock key, and a second cross-worker
exclusion mechanism for what is, in substance, one more thing to check after
a cycle that already ran. Either both the cycle's trading effects and its
drift check persist, or (on a crash) neither does - the same all-or-nothing-
per-deployment posture `runner.py` already documents.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import DriftCheckStatus, StrategyDeployment
from apps.api.app.deployments.monitoring import build_deployment_monitoring


@dataclass(frozen=True)
class DriftCheckResult:
    """The outcome of one drift evaluation. `actual_win_rate_pct` /
    `expected_win_rate_pct` / `win_rate_deviation_pct` are `None` together
    whenever `status is INSUFFICIENT_DATA` - never a guessed number
    computed from too little data (this codebase's standing rule, see
    D058/D075 and `docs/TRADING_SAFETY.md`'s "no fabrication" section).
    `detail` always names the actual numbers involved, never a vague
    "drift detected"."""

    status: DriftCheckStatus
    actual_win_rate_pct: Decimal | None
    expected_win_rate_pct: Decimal | None
    win_rate_deviation_pct: Decimal | None
    num_round_trips: int
    detail: str


async def evaluate_deployment_drift(
    session: AsyncSession,
    deployment: StrategyDeployment,
    *,
    min_round_trips: int,
    max_win_rate_deviation_pct: Decimal,
) -> DriftCheckResult:
    """Compare this deployment's actual win rate to its reference
    backtest's, via `build_deployment_monitoring` (Phase 65, D083) - never
    a second, competing computation of either number.

    `INSUFFICIENT_DATA` when `expected.status != "available"` (no
    reference backtest exists for the strategy version) OR
    `actual.num_round_trips < min_round_trips` (too few closed trades to
    judge). Otherwise `DRIFT_DETECTED` when the absolute deviation between
    the two win rates exceeds `max_win_rate_deviation_pct`, else
    `NO_DRIFT`.
    """
    monitoring = await build_deployment_monitoring(session, deployment)
    actual = monitoring.actual
    expected = monitoring.expected

    if expected.status != "available":
        return DriftCheckResult(
            status=DriftCheckStatus.INSUFFICIENT_DATA,
            actual_win_rate_pct=None,
            expected_win_rate_pct=None,
            win_rate_deviation_pct=None,
            num_round_trips=actual.num_round_trips,
            detail=(
                f"no reference backtest is available for this strategy version "
                f"(expected.status={expected.status!r}); drift cannot be judged "
                f"without a real backtest to compare against."
            ),
        )

    if actual.num_round_trips < min_round_trips:
        return DriftCheckResult(
            status=DriftCheckStatus.INSUFFICIENT_DATA,
            actual_win_rate_pct=None,
            expected_win_rate_pct=None,
            win_rate_deviation_pct=None,
            num_round_trips=actual.num_round_trips,
            detail=(
                f"only {actual.num_round_trips} closed round trip(s) so far, below the "
                f"configured minimum of {min_round_trips}; drift cannot be judged from "
                f"a sample this small."
            ),
        )

    # Both sides are real numbers at this point: `expected.status ==
    # "available"` guarantees `expected.win_rate_pct` is not None (see
    # `deployments/monitoring.py`), and `num_round_trips >= min_round_trips
    # >= 1` (the config validator enforces min_round_trips > 0) guarantees
    # `actual.win_rate_pct` is not None either.
    assert actual.win_rate_pct is not None
    assert expected.win_rate_pct is not None
    actual_rate = actual.win_rate_pct
    expected_rate = expected.win_rate_pct
    deviation = abs(actual_rate - expected_rate)

    if deviation > max_win_rate_deviation_pct:
        return DriftCheckResult(
            status=DriftCheckStatus.DRIFT_DETECTED,
            actual_win_rate_pct=actual_rate,
            expected_win_rate_pct=expected_rate,
            win_rate_deviation_pct=deviation,
            num_round_trips=actual.num_round_trips,
            detail=(
                f"actual win rate {actual_rate}% deviates {deviation}% from the "
                f"reference backtest's {expected_rate}%, exceeding the configured "
                f"maximum of {max_win_rate_deviation_pct}%."
            ),
        )

    return DriftCheckResult(
        status=DriftCheckStatus.NO_DRIFT,
        actual_win_rate_pct=actual_rate,
        expected_win_rate_pct=expected_rate,
        win_rate_deviation_pct=deviation,
        num_round_trips=actual.num_round_trips,
        detail=(
            f"actual win rate {actual_rate}% deviates {deviation}% from the reference "
            f"backtest's {expected_rate}%, within the configured maximum of "
            f"{max_win_rate_deviation_pct}%."
        ),
    )
