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
    SignalDirection,
    StrategyDeploymentRunStatus,
    StrategyDeploymentStatus,
)


class CreateDeploymentRequest(BaseModel):
    """Put a validated version on the paper-trading runner. The version is
    named by the path; this body says which paper broker account it trades,
    which symbols, and at what bar interval.

    `mode` is `Literal["paper"]` - the API rejects anything else until
    Phase 64 wires `live` behind the existing TRADING_MODE / LIVE_TRADING
    gate. It is present now so a reader sees the distinction was always
    intended and so adding `live` needs no schema migration.
    """

    broker_id: uuid.UUID
    symbols: list[str] = Field(min_length=1, max_length=50)
    bar_interval: Literal["1d"] = "1d"
    mode: Literal["paper"] = "paper"


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
