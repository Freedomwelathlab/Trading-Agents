"""DTOs for the Phase 63 strategy-deployment routes (D081).

A deployment has no large child collection - its detail is a handful of
scalars plus a small run list - so there is no summary/detail split; the
list and the by-id read return the same `StrategyDeploymentResponse`.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from apps.api.app.db.models import (
    DriftCheckStatus,
    SignalDirection,
    StrategyDeploymentRunStatus,
    StrategyDeploymentStatus,
)
from apps.api.app.marketdata.bar_provider import BarInterval


class CreateDeploymentRequest(BaseModel):
    """Put a validated version on the scheduled deployment runner. The
    version is named by the path; this body says which broker account it
    trades, which symbols, at what bar interval, and in which mode.

    `mode="live"` (Phase 64, D082) creates a real, approvable deployment
    against a LIVE broker - but the runner never places a live order for
    one; every cycle resolves it straight to `SKIPPED_LIVE_TRADING_DISABLED`
    (see that status's docstring in `db/models.py`). Creating or even
    approving a live deployment today changes nothing about what can
    actually trade - it is scaffolding for a future, separately-approved
    execution path, not a way to reach one now.
    """

    broker_id: uuid.UUID
    symbols: list[str] = Field(min_length=1, max_length=50)
    bar_interval: BarInterval = "1d"
    mode: Literal["paper", "live"] = "paper"


class ApproveDeploymentRequest(BaseModel):
    """The approval action carries no fields today - the actor is the
    authenticated caller and is recorded from the token. A body is accepted
    (and may gain an optional note later) so the endpoint shape is stable."""

    note: str | None = Field(default=None, max_length=500)


class PauseDeploymentRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class StrategyDeploymentResponse(BaseModel):
    id: uuid.UUID
    strategy_version_id: uuid.UUID
    broker_id: uuid.UUID
    mode: str
    status: StrategyDeploymentStatus
    symbols: list[str]
    bar_interval: str
    requested_by_user_id: uuid.UUID | None
    approved_by_user_id: uuid.UUID | None
    approved_at: datetime | None
    paused_reason: str | None
    stopped_at: datetime | None
    last_evaluated_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ListDeploymentsResponse(BaseModel):
    items: list[StrategyDeploymentResponse]
    limit: int
    offset: int


class StrategyDeploymentRunResponse(BaseModel):
    id: uuid.UUID
    deployment_id: uuid.UUID
    status: StrategyDeploymentRunStatus
    started_at: datetime
    completed_at: datetime | None
    symbols_evaluated: int
    signals_actionable: int
    orders_submitted: int
    orders_filled: int
    error_detail: str | None
    created_at: datetime


class ListDeploymentRunsResponse(BaseModel):
    items: list[StrategyDeploymentRunResponse]
    limit: int
    offset: int


class DeploymentSignalResponse(BaseModel):
    """One `SignalEvaluation` a runner cycle produced - the same shape as
    the Phase 61 response, minus the fields a deployment context makes
    redundant."""

    id: uuid.UUID
    deployment_run_id: uuid.UUID | None
    symbol: str
    as_of_bar_date: date | None
    latest_close: Decimal | None
    signal: SignalDirection
    insufficient_data: bool
    explanation: str
    created_at: datetime


class ListDeploymentSignalsResponse(BaseModel):
    items: list[DeploymentSignalResponse]
    limit: int
    offset: int


class RoundTripResponse(BaseModel):
    """One closed position built from this deployment's own real fills -
    see `deployments/monitoring.py::RoundTrip`."""

    symbol: str
    quantity: Decimal
    entry_price: Decimal
    entered_at: datetime
    exit_price: Decimal
    exited_at: datetime
    realized_pnl: Decimal
    return_pct: Decimal


class DeploymentActualPerformanceResponse(BaseModel):
    """What this deployment's own orders actually did. `win_rate_pct` and
    `avg_return_pct` are `null` - never a fabricated 0% or 100% - when
    `num_round_trips` is 0."""

    round_trips: list[RoundTripResponse]
    open_positions: dict[str, Decimal]
    num_round_trips: int
    num_winning: int
    win_rate_pct: Decimal | None
    total_realized_pnl: Decimal
    avg_return_pct: Decimal | None


class DeploymentExpectedPerformanceResponse(BaseModel):
    """What the strategy version's own latest succeeded backtest reported.
    `status == "no_reference_backtest"` means every field below it is
    `null` - there is no partial or estimated fallback."""

    status: Literal["available", "no_reference_backtest"]
    reference_backtest_run_id: uuid.UUID | None
    symbol: str | None
    total_return_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    win_rate_pct: Decimal | None
    num_trades: int | None


class DeploymentMonitoringResponse(BaseModel):
    """Actual vs. expected performance for one deployment - read-only,
    never blended together (Phase 65, D083)."""

    deployment_id: uuid.UUID
    as_of: datetime
    actual: DeploymentActualPerformanceResponse
    expected: DeploymentExpectedPerformanceResponse


class DriftCheckResponse(BaseModel):
    """One append-only `StrategyDriftCheck` row (Phase 66, D084). Every
    successful runner cycle writes one of these - `status ==
    "insufficient_data"` and `status == "no_drift"` rows exist right beside
    `status == "drift_detected"` ones, matching the emergency-stop table's
    "a no-op flip still writes a row" audit philosophy. The three win-rate
    fields are `null` together whenever `status == "insufficient_data"` -
    never a fabricated number from too small a sample."""

    id: uuid.UUID
    deployment_id: uuid.UUID
    status: DriftCheckStatus
    actual_win_rate_pct: Decimal | None
    expected_win_rate_pct: Decimal | None
    win_rate_deviation_pct: Decimal | None
    num_round_trips: int
    action_taken: Literal["none", "observed_only", "paused"]
    detail: str
    created_at: datetime


class ListDriftChecksResponse(BaseModel):
    items: list[DriftCheckResponse]
    limit: int
    offset: int
